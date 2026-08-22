#!/usr/bin/env python3
"""THBR container supervisor: runs the backbone pump, keeps the stick flashed.

Commands (the container's ENTRYPOINT):

  run       default.  Start the TAP pump, relay the stick's UDP log to stdout,
            apply the flash policy once, then supervise the pump forever.
  flash     (re)flash the bundled firmware.  Inside a running container this
            hands the job to the supervisor (which stops the pump first);
            stand-alone it flashes directly.  --force skips the version check.
  version   print bundled and installed firmware versions.
  status    health check: exit 0 when the stick answers through the backbone.

As a Home Assistant add-on the settings come from /data/options.json instead
(the Supervisor writes it); the option names are the environment names without
the THBR_ prefix, lowercased — device, flash, tap, host_addr, stick_addr.

Environment:
  THBR_DEVICE        stick port, use the stable /dev/serial/by-id/... path  (required)
  THBR_TAP           tap interface name                              (tap0)
  THBR_HOST_ADDR     host IPv4/CIDR on the tap                       (192.168.45.1/24)
  THBR_STICK_ADDR    stick IPv4 on the tap                           (192.168.45.2)
  THBR_INFO_PORT     firmware info API port                          (8082)
  THBR_LOG_PORT      UDP port the firmware logs to                   (5514)
  THBR_MTU           backbone MTU                                    (1500)
  THBR_FLASH         auto | upgrade | never                          (auto)
                     auto:    flash only a stick that answers nothing at all
                     upgrade: also replace any other / older firmware
                     never:   never touch the flash
  THBR_PROBE_TIMEOUT seconds to wait for the stick after the pump starts (30)
  THBR_WEB_ALLOW     who may reach the web interface: 'ingress' (Home
                     Assistant only), 'any', or a comma-separated list of
                     addresses/CIDRs.  Defaults to 'ingress' under the
                     Supervisor and 'any' without one.

Flash policy rationale: there is no OTA on the stick and every flash costs the
mesh about a minute of border routing, so a running stick is never reflashed
without being asked (THBR_FLASH=upgrade or `thbrctl flash`).  A stick that
answers nothing is either new, carries an RCP image, or is broken — the one
case where flashing is the obvious next step.
"""
import hashlib
import ipaddress
import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request


HERE = os.path.dirname(os.path.abspath(__file__))
PUMP = os.path.join(HERE, "tap_pump.py")
sys.path.insert(0, HERE)
import webui  # noqa: E402  (needs HERE on the path first)
FW_DIR = os.environ.get("THBR_FIRMWARE_DIR", os.path.join(HERE, "firmware"))
RUN_DIR = "/run/thbr"
LOG_MIRROR = "/run/thbr/thbr.log"          # what the web page shows
REQ_FILE = os.path.join(RUN_DIR, "flash.request")
BACKUP_REQ = os.path.join(RUN_DIR, "backup.request")
BACKUP_RES = os.path.join(RUN_DIR, "backup.result")
RESTORE_REQ = os.path.join(RUN_DIR, "restore.request")
RESTORE_RES = os.path.join(RUN_DIR, "restore.result")
BACKUP_DIR = "/data/backups"
RES_FILE = os.path.join(RUN_DIR, "flash.result")
PID_FILE = os.path.join(RUN_DIR, "supervisor.pid")

ENV = {
    "device": os.environ.get("THBR_DEVICE", ""),
    "tap": os.environ.get("THBR_TAP", "tap0"),
    "host_addr": os.environ.get("THBR_HOST_ADDR", "192.168.45.1/24"),
    "stick": os.environ.get("THBR_STICK_ADDR", "192.168.45.2"),
    "info_port": int(os.environ.get("THBR_INFO_PORT", "8082")),
    "log_port": int(os.environ.get("THBR_LOG_PORT", "5514")),
    "mtu": os.environ.get("THBR_MTU", "1500"),
    "policy": os.environ.get("THBR_FLASH", "auto").lower(),
    "probe_timeout": float(os.environ.get("THBR_PROBE_TIMEOUT", "30")),
    "sysctl_dir": os.environ.get("THBR_SYSCTL_DIR", "/hostsys"),
    "web_allow": os.environ.get("THBR_WEB_ALLOW", ""),
    "release": os.environ.get("THBR_VERSION", ""),
}

# Home Assistant add-on: the Supervisor writes the user's settings here.  They
# win over the environment, which in that context nobody has set.
# How long the kernel gets to install the advertised route before we do it by
# hand.  A router advertisement answering our solicitation arrives in well under
# a second; this is generous on purpose.
ROUTE_GRACE_S = 25.0

WEB_PORT = int(os.environ.get("THBR_WEB_PORT", "8099"))

OPTIONS_FILE = "/data/options.json"
if os.path.exists(OPTIONS_FILE):
    try:
        with open(OPTIONS_FILE) as _fh:
            _opts = json.load(_fh)
        for _key, _env in (("device", "device"), ("flash", "policy"), ("tap", "tap"),
                           ("host_addr", "host_addr"), ("stick_addr", "stick"),
                           ("web_allow", "web_allow")):
            if _opts.get(_key) not in (None, ""):
                ENV[_env] = str(_opts[_key])
        ENV["policy"] = ENV["policy"].lower()
    except (OSError, ValueError) as _e:
        print(f"[thbr] could not read {OPTIONS_FILE}: {_e}", flush=True)


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} [thbr] {msg}"
    print(line, flush=True)
    # Mirrored to a file as well: the add-on log is dominated by the stick's own
    # output, and the web page needs our side of the story.
    try:
        os.makedirs(RUN_DIR, exist_ok=True)
        with open(LOG_MIRROR, "a") as fh:
            fh.write(line + "\n")
        if os.path.getsize(LOG_MIRROR) > 200_000:
            with open(LOG_MIRROR) as fh:
                tail = fh.readlines()[-500:]
            with open(LOG_MIRROR, "w") as fh:
                fh.writelines(tail)
    except OSError:
        pass


# --------------------------------------------------------------------------- firmware bundle

def load_manifest():
    path = os.path.join(FW_DIR, "manifest.json")
    with open(path) as fh:
        m = json.load(fh)
    for img in m["images"]:
        img["path"] = os.path.join(FW_DIR, img["file"])
    return m


def verify_bundle(m):
    """Refuse to flash images that do not match their recorded hashes."""
    for img in m["images"]:
        h = hashlib.sha256()
        with open(img["path"], "rb") as fh:
            h.update(fh.read())
        if h.hexdigest() != img["sha256"]:
            raise SystemExit(f"bundled image {img['file']} is corrupt (sha256 mismatch)")


# --------------------------------------------------------------------------- stick probing

def http_json(url, timeout=3.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def probe():
    """Classify what answers through the backbone.

    Returns ("thbr", info) when the THBR info API answers,
            ("rest", None) when only an ot-br REST API answers on port 80
            (an older THBR build or a foreign OTBR firmware),
            ("none", None) when nothing answers.
    """
    stick = ENV["stick"]
    try:
        info = http_json(f"http://{stick}:{ENV['info_port']}/version")
        if info.get("product") == "THBR":
            return "thbr", info
    except (urllib.error.URLError, OSError, ValueError):
        pass
    try:
        http_json(f"http://{stick}/node/state")
        return "rest", None
    except (urllib.error.URLError, OSError, ValueError):
        pass
    return "none", None


def wait_probe(timeout, pump=None):
    deadline = time.time() + timeout
    while True:
        kind, info = probe()
        if kind != "none":
            return kind, info
        if pump is not None and pump.poll() is not None:
            return "none", None
        if time.time() >= deadline:
            return "none", None
        time.sleep(1.0)


# --------------------------------------------------------------------------- pump

BY_ID = "/dev/serial/by-id"
JTAG_PREFIX = "usb-Espressif_USB_JTAG_serial_debug_unit_"
ADOPTED_MAC = None


def list_candidates():
    """The ports that could be the stick, most recently plugged in first.

    There is no dmesg to consult: /dev/kmsg is not readable from inside a
    container.  But udev creates the by-id symlink when the device appears, so
    the link's own timestamp is when it was last plugged in — which is the
    quickest way to point at the right one of twenty ports: plug the stick in
    last, and it is at the top.

    Returns (path, seconds-since-it-appeared) pairs, Espressif USB-Serial/JTAG
    ports first because that is what THBR runs on.
    """
    try:
        names = [n for n in os.listdir(BY_ID) if n.endswith("-if00")]
    except OSError:
        return []
    now = time.time()
    rows = []
    for n in names:
        path = os.path.join(BY_ID, n)
        try:
            age = now - os.lstat(path).st_mtime
        except OSError:
            age = None
        rows.append((path, age))
    rows.sort(key=lambda r: (r[1] is None, r[1]))
    espressif = [r for r in rows if JTAG_PREFIX in r[0]]
    return espressif or rows


def describe_age(age):
    if age is None:
        return "plugged in at an unknown time"
    if age < 90:
        return f"plugged in {int(age)} s ago"
    if age < 5400:
        return f"plugged in {int(age / 60)} min ago"
    if age < 172800:
        return f"plugged in {age / 3600:.1f} h ago"
    return f"plugged in {age / 86400:.1f} days ago"


def by_id_name(path):
    """The /dev/serial/by-id name of whatever was configured.

    The device picker may hand over a bare /dev/ttyACM3, which says nothing
    about which device it is and changes when the stick re-enumerates. The
    by-id name does say: for the C6's native USB port it carries the chip's own
    MAC as the USB serial number. So resolve back to it wherever possible.
    """
    base = os.path.basename(path)
    if base.startswith("usb-"):
        return base
    try:
        real = os.path.realpath(path)
        for name in sorted(os.listdir(BY_ID)):
            if os.path.realpath(os.path.join(BY_ID, name)) == real:
                return name
    except OSError:
        pass
    return None


def mac_from_port(path):
    """The MAC a USB-Serial/JTAG port announces, or None if it is not one.

    This is the base MAC — the form esptool prints as BASE MAC, not the EUI-64
    with ff:fe in the middle that a C6 reports as its MAC.
    """
    m = re.search(re.escape(JTAG_PREFIX) + r"([0-9A-Fa-f:]{17})", by_id_name(path) or "")
    return m.group(1).lower() if m else None


def port_for_mac(mac):
    """The port that currently carries this chip, whatever its ttyACM number."""
    try:
        for name in sorted(os.listdir(BY_ID)):
            if name.endswith("-if00") and mac_from_port(name) == mac:
                return os.path.join(BY_ID, name)
    except OSError:
        pass
    return None


def normalise_chip(name):
    return name.strip().lower().replace("-", "").replace("_", "")


APP_DESC_MAGIC = 0xABCD5432


def parse_app_desc(blob):
    """(project, version) out of an ESP-IDF application image, else None.

    Every IDF application carries an esp_app_desc_t right behind the image
    header, at offset 0x20, naming the project it was built as.  That name is
    the only thing that tells two boards apart once they are both an ESP32-C6
    on an Espressif USB-Serial/JTAG port — which a CUL32 and a TUL32 are.
    """
    if len(blob) < 0xA0:
        return None
    magic, = struct.unpack_from("<I", blob, 0x20)
    if magic != APP_DESC_MAGIC:
        return None
    def field(off, size):
        return blob[off:off + size].split(b"\0")[0].decode("utf-8", "replace")
    return field(0x50, 32), field(0x30, 32)


def bundled_app(m):
    """(offset, project, version) of the application among the bundled images.

    Read out of the image that is about to be written rather than configured
    anywhere: whatever this add-on ships is by definition what it may replace.
    """
    for img in m["images"]:
        try:
            with open(img["path"], "rb") as fh:
                head = fh.read(0xA0)
        except OSError:
            continue
        desc = parse_app_desc(head)
        if desc:
            return int(img["offset"], 16), desc[0], desc[1]
    return None, None, None


def _esptool_identity(out):
    m = (re.search(r"Detecting chip type\.\.\.\s*(\S+)", out)
         or re.search(r"Connected to (\S+) on", out))
    chip = normalise_chip(m.group(1)) if m else None
    m = re.search(r"BASE MAC:\s*([0-9a-fA-F:]{17})", out)
    return chip, (m.group(1).lower() if m else None)


def _esptool(cmd, timeout=120):
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, timeout=timeout)
        return proc.returncode, proc.stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, f"{type(e).__name__}: {e}"


def inspect_target(port, app_offset=None):
    """Ask a port what chip is on it and what application it carries.

    Reads only.  One esptool run answers both questions: every command prints
    the chip it detected and its MAC, and the head of the app slot yields the
    application descriptor.

    Asking costs the device a reset — there is no way to ask a chip anything
    without one — so it is always reset back out of the bootloader afterwards.
    A device we have just decided not to touch must be left as it was found.

    Returns (chip, mac, project, app_version, error).
    """
    if app_offset is not None:
        tmp = "/tmp/thbr-appdesc.bin"
        rc, out = _esptool([sys.executable, "-m", "esptool", "--port", port,
                            "--baud", "460800", "--before", "default-reset",
                            "--after", "hard-reset",
                            "read-flash", hex(app_offset), "0x100", tmp])
        if rc == 0:
            chip, mac = _esptool_identity(out)
            blob = b""
            try:
                with open(tmp, "rb") as fh:
                    blob = fh.read()
                os.unlink(tmp)
            except OSError:
                pass
            desc = parse_app_desc(blob)
            if chip:
                return chip, mac, (desc[0] if desc else None), (desc[1] if desc else None), None
    # Reading the flash failed, or said nothing about the chip.  Fall back to
    # the smallest question there is, so that at least the chip is established.
    rc, out = _esptool([sys.executable, "-m", "esptool", "--port", port,
                        "--baud", "115200", "--before", "default-reset",
                        "--after", "hard-reset", "--no-stub", "read-mac"])
    chip, mac = _esptool_identity(out)
    if rc != 0 or not chip:
        tail = [ln.strip() for ln in out.splitlines() if ln.strip()][-2:]
        return None, None, None, None, "; ".join(tail) or f"esptool exit {rc}"
    return chip, mac, None, None, None


def wait_for_configuration():
    """No device configured: say what is plugged in and wait, rather than
    exiting into a restart loop where the log scrolls past before anyone can
    read it."""
    while True:
        cands = list_candidates()
        if cands:
            log("no device configured.  These look like candidates, most "
                "recently plugged in first:")
            for path, age in cands:
                log(f"    {path}   ({describe_age(age)})")
        else:
            log("no device configured, and nothing under /dev/serial/by-id — "
                "is the stick plugged in, and does this container see it?")
        log("set the 'device' option (add-on) or THBR_DEVICE (docker) and restart.")
        time.sleep(60)


def wait_for_device(path, timeout=60):
    deadline = time.time() + timeout
    warned = False
    while not os.path.exists(path):
        if not warned:
            log(f"waiting for {path} (not present)")
            warned = True
        # A port configured as a bare /dev/ttyACM3 can come back under another
        # number after a flash or a replug.  The chip's MAC does not move, so
        # look that same chip up again rather than waiting for a name that is
        # not going to return.
        if ADOPTED_MAC:
            other = port_for_mac(ADOPTED_MAC)
            if other and os.path.realpath(other) != os.path.realpath(path):
                log(f"{path} is gone; the same chip ({ADOPTED_MAC}) is now at {other}")
                ENV["device"] = other
                return True
        if time.time() >= deadline:
            return False
        time.sleep(1.0)
    return True


def adopt_port():
    """Note which chip the configured port belongs to, and say so in the log.

    Costs nothing and opens nothing: the name under /dev/serial/by-id already
    carries the chip's MAC.  Worth saying out loud, because picking the device
    from a list of serial ports is the one step where a user can silently point
    this add-on at something else entirely.
    """
    global ADOPTED_MAC
    ADOPTED_MAC = mac_from_port(ENV["device"])
    if ADOPTED_MAC:
        log(f"stick port {ENV['device']} — USB-Serial/JTAG of chip {ADOPTED_MAC}")
    else:
        log(f"stick port {ENV['device']} — this is not an Espressif "
            "USB-Serial/JTAG port.  THBR runs on the C6's own USB port; nothing "
            "is written to this one until it has been asked what it is.")


PUMP_STARTED = 0.0


def start_pump():
    global PUMP_STARTED
    PUMP_STARTED = time.time()
    cmd = [sys.executable, PUMP,
           "--dev", ENV["device"], "--tap", ENV["tap"], "--addr", ENV["host_addr"],
           "--mtu", ENV["mtu"], "--stats", "3600", "--persist"]
    if os.path.isdir(ENV["sysctl_dir"]):
        cmd += ["--sysctl-dir", ENV["sysctl_dir"]]
    log("starting pump: " + " ".join(cmd[2:]))
    return subprocess.Popen(cmd)


def run_ip(args, check=False):
    """Run an `ip` command; return (rc, output)."""
    proc = subprocess.run(["ip"] + args, capture_output=True, text=True)
    if check and proc.returncode != 0:
        log(f"ip {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def eui64_address(prefix, mac):
    """The address SLAAC would derive for `mac` inside `prefix`."""
    b = bytearray(int(x, 16) for x in mac.split(":"))
    iid = bytes([b[0] ^ 0x02, b[1], b[2], 0xFF, 0xFE, b[3], b[4], b[5]])
    net = ipaddress.IPv6Network(prefix, strict=False)
    return ipaddress.IPv6Address(int(net.network_address) | int.from_bytes(iid, "big"))


def ensure_backbone_routing():
    """Make sure the host can actually reach the Thread mesh.

    Normally the kernel does this from the border router's advertisements, but
    that needs two per-interface sysctls, and inside a container /proc/sys is
    read-only — where the host tree is not mapped in, the route information is
    silently discarded and everything above IP looks broken for no visible
    reason.  The firmware reports the prefixes it advertises, so rather than
    fail quietly we install what is missing and say that we did.
    """
    tap = ENV["tap"]
    # Give the kernel first refusal.  Where the sysctls could be set it learns
    # all of this from the router advertisement, with the lifetimes the border
    # router intends — better than anything we can staple on.  Only once that
    # has demonstrably not happened do we step in.
    if time.time() - PUMP_STARTED < ROUTE_GRACE_S:
        return
    try:
        bb = http_json(f"http://{ENV['stick']}:{ENV['info_port']}/backbone")
    except (urllib.error.URLError, OSError, ValueError):
        return
    omr, onlink, ll = bb.get("omr_prefix"), bb.get("onlink_prefix"), bb.get("ll")
    if not omr or not ll:
        return          # border router has not published its prefixes yet

    # 1. an address inside the on-link prefix, so replies from the mesh find
    #    their way back (this is what SLAAC would have given us)
    if onlink:
        _, have = run_ip(["-6", "addr", "show", "dev", tap])
        net = ipaddress.IPv6Network(onlink, strict=False)
        mine = []
        for ln in have.splitlines():
            if "inet6" not in ln or "scope global" not in ln:
                continue
            cidr = ln.split()[1]
            try:
                addr = ipaddress.IPv6Address(cidr.split("/")[0])
            except ValueError:
                continue
            if addr in net:
                mine.append(ln)
                continue
            # An address left over from a prefix the border router no longer
            # advertises.  It has to go: the kernel may still pick it as the
            # source for traffic into the mesh, and the replies would have no
            # way back — the mesh then looks unreachable while every other
            # check says the link is fine.  Only addresses we set ourselves are
            # removed; anything the kernel autoconfigured expires on its own.
            if "dynamic" in ln or "mngtmpaddr" in ln:
                continue
            rc, _ = run_ip(["-6", "addr", "del", cidr, "dev", tap])
            if rc == 0:
                log(f"removed stale address {cidr} on {tap} — the border router now "
                    f"advertises {onlink}")
        if not mine:
            try:
                with open(f"/sys/class/net/{tap}/address") as fh:
                    mac = fh.read().strip()
                addr = eui64_address(onlink, mac)
                rc, _ = run_ip(["-6", "addr", "replace", f"{addr}/{net.prefixlen}",
                                "dev", tap], check=True)
                if rc == 0:
                    log(f"added {addr}/{net.prefixlen} on {tap} — the kernel did not "
                        f"autoconfigure one from the router advertisement")
            except (OSError, ValueError) as e:
                log(f"could not add an on-link address on {tap}: {e}")

    # 2. the route into the mesh.  A prefix change would otherwise leave the old
    #    route behind as a black hole, so anything we installed earlier and that
    #    is not the current prefix goes first.
    _, existing = run_ip(["-6", "route", "show", "dev", tap, "proto", "static"])
    for line in existing.splitlines():
        dst = line.split()[0] if line.split() else ""
        if dst and dst != omr:
            run_ip(["-6", "route", "del", dst, "dev", tap])
            log(f"removed stale route {dst} on {tap} (border router now advertises {omr})")

    _, route = run_ip(["-6", "route", "show", omr, "dev", tap])
    if "proto ra" in route:
        # The kernel took the advertisement after all; drop our stand-in so the
        # route follows the border router's lifetimes again.
        if "proto static" in route:
            run_ip(["-6", "route", "del", omr, "dev", tap, "proto", "static"])
            log(f"removed the hand-installed {omr} — the kernel now has it from "
                f"the router advertisement")
        return
    if route.strip():
        return
    rc, _ = run_ip(["-6", "route", "replace", omr, "via", ll, "dev", tap,
                    "proto", "static", "metric", "1024"], check=True)
    if rc == 0:
        log(f"installed {omr} via {ll} on {tap} by hand — the kernel did not take "
            f"the route from the router advertisement (accept_ra_rt_info_max_plen)")


def verify_mesh_reachable():
    """Ping something inside the mesh, so the log says whether the route works
    rather than only that it exists.  The border router's own address in the
    advertised prefix is the closest target that proves the whole path:
    tap -> SLIP -> stick -> Thread.
    """
    try:
        bb = http_json(f"http://{ENV['stick']}:{ENV['info_port']}/backbone")
        omr = bb.get("omr_prefix")
        if not omr:
            return
        net = ipaddress.IPv6Network(omr, strict=False)
        nodes = http_json(f"http://{ENV['stick']}/diagnostics", timeout=15.0)
    except (urllib.error.URLError, OSError, ValueError):
        return
    target = None
    for node in nodes if isinstance(nodes, list) else []:
        for a in node.get("IP6AddressList", []):
            try:
                if ipaddress.IPv6Address(a) in net:
                    target = a
                    break
            except ValueError:
                continue
        if target:
            break
    if not target:
        return
    proc = subprocess.run(["ping", "-6", "-c", "2", "-W", "3", target],
                          capture_output=True, text=True)
    if proc.returncode == 0:
        rtt = ""
        for line in proc.stdout.splitlines():
            if "min/avg/max" in line:
                rtt = " (" + line.split("=")[-1].strip() + ")"
        log(f"mesh reachable: {target} answers{rtt}")
        webui.CTX["mesh_text"] = f"{target}{rtt}"
        webui.CTX["mesh_ok"] = True
        return True
    log(f"MESH NOT REACHABLE: {target} does not answer, although the route is "
        f"in place — check that nothing filters traffic on {ENV['tap']}")
    webui.CTX["mesh_text"] = f"{target} does not answer"
    webui.CTX["mesh_ok"] = False
    return False


def reboot_stick():
    """Last resort: the border router answers on the backbone and still routes
    nothing into the mesh.  Nothing short of a restart has been observed to
    clear that, and there is no other way in — so ask the firmware to restart."""
    try:
        req = urllib.request.Request(
            f"http://{ENV['stick']}:{ENV['info_port']}/reboot", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
        log("asked the stick to restart (mesh unreachable for three checks)")
        return True
    except (urllib.error.URLError, OSError) as e:
        log(f"could not ask the stick to restart: {e}")
        return False


def send_rs():
    """Ask the border router for a router advertisement now.  The kernel
    solicits only when the tap comes up; a persistent tap that survived a pump
    restart would otherwise wait for the router's periodic advertisement."""
    tap = ENV["tap"]
    try:
        idx = socket.if_nametoindex(tap)
        s = socket.socket(socket.AF_INET6, socket.SOCK_RAW, socket.IPPROTO_ICMPV6)
        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, 255)
        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_IF, idx.to_bytes(4, sys.byteorder))
        s.sendto(bytes([133, 0, 0, 0, 0, 0, 0, 0]), ("ff02::2", 0, 0, idx))   # ICMPv6 RS
        s.close()
    except OSError as e:
        log(f"could not send router solicitation on {tap}: {e}")


def after_pump_start():
    time.sleep(2.0)
    check_sysctls()
    send_rs()
    # The routing check needs the border router to be up, so it is repeated in
    # the supervisor loop rather than only here.
    ensure_backbone_routing()


def check_sysctls():
    """The route into the mesh depends on two per-interface sysctls the pump
    sets; /proc/sys is read-only in a container unless the host's conf tree is
    mapped in.  Reading works regardless, so verify and say what is missing."""
    tap = ENV["tap"]
    want = {"accept_ra": "2", "accept_ra_rt_info_max_plen": "64"}
    bad = []
    for key, val in want.items():
        try:
            with open(f"/proc/sys/net/ipv6/conf/{tap}/{key}") as fh:
                have = fh.read().strip()
        except OSError:
            continue
        if have != val:
            bad.append(f"{key}={have} (want {val})")
    if bad:
        log(f"WARNING {tap} sysctls not set: {', '.join(bad)} — the host will not learn the "
            f"route into the Thread mesh.  Map the host's conf tree into the container: "
            f"-v /proc/sys/net/ipv6/conf:{ENV['sysctl_dir']}  (see compose.yaml)")
    return not bad


def stop_pump(p):
    if p is None or p.poll() is not None:
        return
    p.terminate()
    try:
        p.wait(5)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()


# --------------------------------------------------------------------------- firmware log relay

def log_relay():
    """Print the stick's UDP log lines on our stdout, so `docker logs` has them."""
    host_ip = ENV["host_addr"].split("/")[0]
    sock = None
    while sock is None:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host_ip, ENV["log_port"]))
            sock = s
        except OSError:
            time.sleep(2.0)      # tap not up yet
    # The firmware's sink sends fixed-size chunks, not lines: reassemble at
    # newlines and flush a dangling tail after a short silence.
    sock.settimeout(0.5)
    tail = ""
    while True:
        try:
            data, _ = sock.recvfrom(2048)
        except socket.timeout:
            if tail.strip():
                print("[stick]", tail.rstrip(), flush=True)
            tail = ""
            continue
        text = tail + data.decode("utf-8", "replace")
        lines = text.split("\n")
        tail = lines.pop()
        for line in lines:
            if line.strip():
                print("[stick]", line.rstrip(), flush=True)


# --------------------------------------------------------------------------- flashing

def check_target(m, force=False):
    """Refuse to write to anything that is not this stick.

    The device is picked by hand from a list of serial ports, and writing to
    the wrong one is the single mistake this add-on could make that a user
    cannot undo.  Three questions, in the order that makes the answer cheapest:

      the port    is it named after a chip at all, and after this one?
      the chip    is it the chip the bundled firmware is built for?
      the app     is what runs there something this add-on may replace?

    The third is the one that matters on a bench: a CUL32 and a TUL32 are
    ESP32-C6 boards on an Espressif USB-Serial/JTAG port, exactly like this
    stick, and neither the port name nor the chip type tells them apart.  What
    does is the name their application was built under.
    """
    port = ENV["device"]
    app_offset, our_project, our_version = bundled_app(m)
    chip, mac, project, version, err = inspect_target(port, app_offset)

    if chip is None:
        log(f"NOT flashing: nothing on {port} answers as an Espressif chip — {err}")
        log("    Check the 'device' setting.  It must be the stick's own USB "
            "port; another serial device on this machine is left untouched.")
        return False

    if chip != normalise_chip(m["chip"]):
        log(f"NOT flashing: {port} holds an {chip}, the bundled firmware is for "
            f"{m['chip']}.")
        log("    Either the wrong device is configured, or this board is not one "
            "this firmware fits.")
        return False

    named = mac_from_port(port)
    if named and mac and named != mac:
        log(f"NOT flashing: {port} is named after {named} but the chip answers "
            f"{mac} — the path points somewhere else than it claims.")
        return False

    if project and our_project and project != our_project:
        if not force:
            carries = f"'{project}'" + (f" {version}" if version else "")
            log(f"NOT flashing: {port} carries the application {carries}, not "
                f"'{our_project}'.")
            log("    That is a different board with the same chip — another "
                "busware stick, an ESPHome node.  Nothing is written over an "
                "application this add-on did not build.")
            log("    If this really is the stick to convert, ask for it: "
                "`thbrctl flash --force`, or the update button on the add-on's "
                "page.")
            return False
        log(f"forced: replacing the application '{project}' on {port}")

    known = f"target confirmed: {chip}"
    if mac:
        known += f", MAC {mac}"
    if project:
        known += f", carrying '{project}'" + (f" {version}" if version else "")
    log(known)
    return True


def flash(m, force=False):
    """Write all bundled images.  The port must be free (pump stopped)."""
    verify_bundle(m)
    if not check_target(m, force):
        return False
    cmd = [sys.executable, "-m", "esptool",
           "--chip", m["chip"], "--port", ENV["device"], "--baud", "921600",
           "--before", "default-reset", "--after", "hard-reset",
           "write-flash"] + m["flash_args"].split()
    for img in m["images"]:
        cmd += [img["offset"], img["path"]]
    log(f"flashing {m['fw']} ({len(m['images'])} images) to {ENV['device']}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    verified = 0
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            print("[esptool]", line, flush=True)
        if "Hash of data verified" in line:
            verified += 1
    rc = proc.wait()
    ok = rc == 0 and verified >= len(m["images"])
    log(f"flash {'OK' if ok else 'FAILED'}: exit={rc} verified_images={verified}/{len(m['images'])}")
    return ok


def backup_nvs():
    """Copy the stick's NVS partition to a file.

    NVS is what makes the stick *this* border router: the Thread dataset it
    rejoins with and the prefix it advertises.  Everything else on the flash is
    the firmware, which this add-on already carries.  Reading it needs the
    serial port, so the pump has to stand aside for the moment it takes — a
    24 KB partition reads in well under a second.  (A whole-flash read does not
    work over USB-Serial/JTAG: measured, 4 MB aborts with "Packet content
    transfer stopped", while this one is instant.)
    """
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(BACKUP_DIR, f"nvs-{stamp}.bin")
    cmd = [sys.executable, "-m", "esptool", "--chip", "esp32c6",
           "--port", ENV["device"], "--before", "default-reset",
           "--after", "hard-reset", "read-flash", "0x9000", "0x6000", path]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if proc.returncode == 0 and os.path.exists(path) and os.path.getsize(path) == 0x6000:
        log(f"saved the stick's network settings to {os.path.basename(path)}")
        return os.path.basename(path)
    log(f"could not read the network settings: {(proc.stderr or proc.stdout)[:150]}")
    try:
        os.unlink(path)
    except OSError:
        pass
    return ""


def restore_nvs(name):
    """Write a saved NVS image back onto the stick.

    This is what the saving is for: a stick that died is replaced, its network
    settings are written onto the new one, and the Thread network carries on
    with the same credentials — the devices in it never notice.
    """
    path = os.path.join(BACKUP_DIR, os.path.basename(name))
    if not os.path.exists(path) or os.path.getsize(path) != 0x6000:
        log(f"cannot restore {name}: not a 24 KB settings file")
        return "no such backup"
    cmd = [sys.executable, "-m", "esptool", "--chip", "esp32c6",
           "--port", ENV["device"], "--before", "default-reset",
           "--after", "hard-reset", "write-flash", "0x9000", path]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    ok = proc.returncode == 0 and "Hash of data verified" in proc.stdout
    log(f"restored the network settings from {os.path.basename(path)}: "
        f"{'OK' if ok else 'FAILED'}")
    return "OK" if ok else "FAILED"


def decide(kind, info, m):
    """Return a reason string when the policy says flash, else None."""
    pol = ENV["policy"]
    if pol == "never":
        return None
    if kind == "thbr":
        if info.get("chip") and info["chip"] != m["chip"]:
            log(f"installed firmware is for {info['chip']}, bundle is for {m['chip']} — not flashing")
            return None
        if pol == "upgrade" and info.get("fw") != m["fw"]:
            return f"installed {info.get('fw')} != bundled {m['fw']}"
        return None
    if kind == "rest":
        if pol == "upgrade":
            return "border-router REST without THBR info API (older THBR or foreign OTBR)"
        log("a border-router REST API answers on port 80 but no THBR info API: "
            "older THBR build or foreign OTBR firmware.  Left alone; "
            "set THBR_FLASH=upgrade or run `thbrctl flash` to replace it.")
        return None
    # nothing answers
    return "stick answers nothing through the backbone"


def flash_cycle(pump, m, reason, force=False):
    log("flash: " + reason)
    stop_pump(pump)
    time.sleep(1.0)
    ok = flash(m, force)
    time.sleep(2.0)                      # re-enumeration after hard-reset
    wait_for_device(ENV["device"], 30)
    pump = start_pump()
    kind, info = wait_probe(ENV["probe_timeout"] + 30, pump)
    if kind == "thbr":
        send_rs()
    if kind == "thbr":
        log(f"stick answers: THBR {info.get('fw')} ({info.get('build')})")
    else:
        log(f"after flashing the stick still answers '{kind}' — check the device and docker logs")
    return pump, ok and kind == "thbr"


# --------------------------------------------------------------------------- commands

def cmd_run():
    if not ENV["device"]:
        wait_for_configuration()
    if ENV["policy"] not in ("auto", "upgrade", "never"):
        raise SystemExit(f"THBR_FLASH={ENV['policy']} is not one of auto|upgrade|never")
    m = load_manifest()
    log(f"THBR {ENV['release'] or 'dev'}; bundled firmware {m['fw']} "
        f"({m['build']}, {m['chip']}); policy {ENV['policy']}")
    adopt_port()
    os.makedirs(RUN_DIR, exist_ok=True)
    for f in (REQ_FILE, RES_FILE):
        if os.path.exists(f):
            os.unlink(f)
    with open(PID_FILE, "w") as fh:
        fh.write(str(os.getpid()))

    threading.Thread(target=log_relay, daemon=True).start()

    # The web face, served through Home Assistant's ingress.
    try:
        webui.CTX.update(backup_req=BACKUP_REQ, backup_res=BACKUP_RES,
                         backup_dir=BACKUP_DIR, restore_req=RESTORE_REQ,
                         restore_res=RESTORE_RES)
        # Under the Supervisor the page is reached through ingress, which
        # authenticates; the raw port is on every host interface only because
        # the pump needs the host's network namespace, and nothing but ingress
        # has business there.  Without a Supervisor there is no ingress and the
        # port is the only way in, so it stays open — and says so.
        allow = ENV["web_allow"] or ("ingress" if os.environ.get("SUPERVISOR_TOKEN")
                                     else "any")
        webui.start(ENV, REQ_FILE, RES_FILE, LOG_MIRROR, m["fw"], WEB_PORT, allow,
                    ENV["release"])
        if webui.parse_allow(allow) is None:
            log(f"web interface on port {WEB_PORT}, reachable from anywhere this "
                "host is.  It can flash the stick and hand out the Thread "
                "credentials, so put it behind something or set THBR_WEB_ALLOW "
                "to a network you trust.")
        else:
            log(f"web interface on port {WEB_PORT} (Home Assistant ingress; "
                f"other sources refused)")
    except OSError as e:
        log(f"could not start the web interface on port {WEB_PORT}: {e}")

    if not wait_for_device(ENV["device"], 60):
        log(f"{ENV['device']} did not appear — is the stick plugged in and /dev mapped?")
    pump = start_pump()
    after_pump_start()
    kind, info = wait_probe(ENV["probe_timeout"], pump)
    if kind == "thbr":
        log(f"stick answers: THBR {info.get('fw')} ({info.get('build')})")
    elif kind == "rest":
        log("stick answers on port 80 only (no THBR info API)")
    else:
        log(f"no answer from {ENV['stick']} within {ENV['probe_timeout']:.0f}s")

    reason = None
    if pump.poll() is None:
        reason = decide(kind, info, m)
    elif kind == "none":
        log("pump exited — not flashing on a dead link (wrong THBR_DEVICE?)")
    if reason:
        pump, _ = flash_cycle(pump, m, reason)

    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(1))
    last_status = time.time()
    last_check = time.time()
    last_probe = time.time()
    silent = 0
    # First reachability report soon after start, then every five minutes.
    last_check_reach = time.time() - 240
    unreachable = 0
    while not stopping:
        time.sleep(1.0)
        if pump.poll() is not None:
            log(f"pump exited (rc={pump.returncode}) — restarting in 3s")
            time.sleep(3.0)
            wait_for_device(ENV["device"], 60)
            pump = start_pump()
            after_pump_start()
        if os.path.exists(RESTORE_REQ):
            with open(RESTORE_REQ) as fh:
                wanted = fh.read().strip()
            os.unlink(RESTORE_REQ)
            stop_pump(pump)
            time.sleep(1.0)
            result = restore_nvs(wanted)
            time.sleep(2.0)
            wait_for_device(ENV["device"], 60)
            pump = start_pump()
            after_pump_start()
            with open(RESTORE_RES + ".tmp", "w") as fh:
                fh.write(result)
            os.replace(RESTORE_RES + ".tmp", RESTORE_RES)
        if os.path.exists(BACKUP_REQ):
            os.unlink(BACKUP_REQ)
            stop_pump(pump)
            time.sleep(1.0)
            name = backup_nvs()
            time.sleep(1.0)
            wait_for_device(ENV["device"], 30)
            pump = start_pump()
            after_pump_start()
            with open(BACKUP_RES + ".tmp", "w") as fh:
                fh.write(name)
            os.replace(BACKUP_RES + ".tmp", BACKUP_RES)
        if os.path.exists(REQ_FILE):
            with open(REQ_FILE) as fh:
                force = fh.read().strip() == "force"
            os.unlink(REQ_FILE)
            kind, info = probe()
            if not force and kind == "thbr" and info.get("fw") == m["fw"]:
                result = f"installed firmware is already {m['fw']} — use --force to reflash"
            else:
                pump, ok = flash_cycle(pump, m, "requested" + (" (forced)" if force else ""), force)
                result = "OK" if ok else "FAILED"
            with open(RES_FILE + ".tmp", "w") as fh:
                fh.write(result)
            os.replace(RES_FILE + ".tmp", RES_FILE)
        # Second line of defence: the pump can be alive and still be talking to
        # a descriptor that died with the port.  If the stick stays silent while
        # the pump reports no trouble, restart the link rather than sit in a
        # state that looks healthy from the outside.
        if time.time() - last_probe >= 60:
            last_probe = time.time()
            kind, _ = probe()
            if kind == "none":
                silent += 1
                if silent == 3:
                    log("stick silent for 3 minutes — restarting the pump")
                    stop_pump(pump)
                    wait_for_device(ENV["device"], 60)
                    pump = start_pump()
                    after_pump_start()
                    silent = 0
            else:
                silent = 0
        if time.time() - last_check >= 15:
            last_check = time.time()
            ensure_backbone_routing()
        if time.time() - last_check_reach >= 300:
            last_check_reach = time.time()
            if verify_mesh_reachable() is False:
                unreachable += 1
                if unreachable >= 3:
                    unreachable = 0
                    if reboot_stick():
                        time.sleep(20)
                        wait_for_device(ENV["device"], 60)
                        stop_pump(pump)
                        pump = start_pump()
                        after_pump_start()
            else:
                unreachable = 0
        if time.time() - last_status >= 600:
            last_status = time.time()
            try:
                st = http_json(f"http://{ENV['stick']}:{ENV['info_port']}/status")
                log("status: " + " ".join(f"{k}={v}" for k, v in st.items()))
            except (urllib.error.URLError, OSError, ValueError):
                log("status: stick not answering")
    log("stopping")
    stop_pump(pump)


def supervisor_alive():
    try:
        with open(PID_FILE) as fh:
            pid = int(fh.read().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def cmd_flash(args):
    force = "--force" in args
    m = load_manifest()
    if supervisor_alive():
        if os.path.exists(RES_FILE):
            os.unlink(RES_FILE)
        with open(REQ_FILE + ".tmp", "w") as fh:
            fh.write("force" if force else "")
        os.replace(REQ_FILE + ".tmp", REQ_FILE)
        log("flash request handed to the supervisor, waiting ...")
        deadline = time.time() + 300
        while time.time() < deadline:
            if os.path.exists(RES_FILE):
                with open(RES_FILE) as fh:
                    result = fh.read().strip()
                log("result: " + result)
                return 0 if result == "OK" else 1
            time.sleep(1.0)
        log("no result within 300s")
        return 1
    if not ENV["device"]:
        raise SystemExit("THBR_DEVICE is not set")
    if not wait_for_device(ENV["device"], 10):
        raise SystemExit(f"{ENV['device']} not present")
    return 0 if flash(m) else 1


def cmd_version():
    m = load_manifest()
    print(f"bundled:   THBR {m['fw']} ({m['build']}, {m['chip']})")
    kind, info = probe()
    if kind == "thbr":
        print(f"installed: THBR {info.get('fw')} ({info.get('build')}, {info.get('chip')}, mac {info.get('mac')})")
    elif kind == "rest":
        print("installed: a border-router REST API answers, but no THBR info API")
    else:
        print("installed: no answer from the stick")
    return 0


def cmd_status():
    tap = ENV["tap"]
    if not os.path.exists(f"/sys/class/net/{tap}"):
        print(f"{tap} missing")
        return 1
    kind, info = probe()
    if kind == "none":
        print("stick not answering")
        return 1
    print(f"ok ({kind}{' ' + info.get('fw', '') if info else ''})")
    return 0


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "run"
    if cmd == "run":
        cmd_run()
        return 0
    if cmd == "flash":
        return cmd_flash(argv[2:])
    if cmd == "version":
        return cmd_version()
    if cmd == "status":
        return cmd_status()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
