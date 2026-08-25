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
  THBR_MATTER_ADDR   Matter server the stick's BLE proxy dials, host:port,
                     empty to switch the forwarder off        (127.0.0.1:5580)
  THBR_MATTER_PORT   port the forwarder offers on the tap                (5580)
  THBR_STICK_LOG     how much of the stick's own log to repeat  (quiet|all|off)
                     quiet: drop the border router's routine web/diagnostics
                     chatter, which is most of it, and keep everything else
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
import base64
import errno
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
    "matter_addr": os.environ.get("THBR_MATTER_ADDR", "127.0.0.1:5580"),
    "matter_port": int(os.environ.get("THBR_MATTER_PORT", "5580")),
    "stick_log": os.environ.get("THBR_STICK_LOG", "quiet").lower(),
}

# Home Assistant add-on: the Supervisor writes the user's settings here.  They
# win over the environment, which in that context nobody has set.
# How long the kernel gets to install the advertised route before we do it by
# hand.  A router advertisement answering our solicitation arrives in well under
# a second; this is generous on purpose.
ROUTE_GRACE_S = 25.0

# How often the forwarder looks for the Matter server it is meant to reach,
# both before it takes a port and when a bind is contested.
FORWARDER_PROBE_S = 15.0

WEB_PORT = int(os.environ.get("THBR_WEB_PORT", "8099"))

OPTIONS_FILE = "/data/options.json"
if os.path.exists(OPTIONS_FILE):
    try:
        with open(OPTIONS_FILE) as _fh:
            _opts = json.load(_fh)
        for _key, _env in (("device", "device"), ("flash", "policy"), ("tap", "tap"),
                           ("host_addr", "host_addr"), ("stick_addr", "stick"),
                           ("web_allow", "web_allow"),
                           ("stick_log", "stick_log")):
            if _opts.get(_key) not in (None, ""):
                ENV[_env] = str(_opts[_key])
        ENV["policy"] = ENV["policy"].lower()
    except (OSError, ValueError) as _e:
        print(f"[thbr] could not read {OPTIONS_FILE}: {_e}", flush=True)


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} [thbr] {msg}"
    # One write, newline included: print() emits the text and the newline
    # separately, and two threads logging at once then run into each other —
    # which they now do, the forwarder having a thread of its own.
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
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

def _read_manifest(d):
    with open(os.path.join(d, "manifest.json")) as fh:
        m = json.load(fh)
    for img in m["images"]:
        img["path"] = os.path.join(d, img["file"])
    return m


def bundle_chips():
    """Every firmware in the image, by the chip it is built for.

    One stick is an ESP32-C6, another a C5, and the same add-on serves both —
    so the bundle is a directory per chip.  A flat bundle (one manifest beside
    its images) is still read as it always was; that is what a single-chip
    build produces and what earlier images contain.
    """
    if os.path.exists(os.path.join(FW_DIR, "manifest.json")):
        m = _read_manifest(FW_DIR)
        return {normalise_chip(m["chip"]): m}
    out = {}
    for name in sorted(os.listdir(FW_DIR)):
        d = os.path.join(FW_DIR, name)
        if os.path.exists(os.path.join(d, "manifest.json")):
            m = _read_manifest(d)
            out[normalise_chip(m["chip"])] = m
    return out


def load_manifest(chip=None):
    """The firmware to work with: the one for this chip, or the only one there is."""
    bundles = bundle_chips()
    if not bundles:
        raise SystemExit(f"no firmware bundle in {FW_DIR}")
    if chip and normalise_chip(chip) in bundles:
        return bundles[normalise_chip(chip)]
    if len(bundles) == 1:
        return next(iter(bundles.values()))
    if chip:
        raise SystemExit(f"no bundled firmware for an {chip}; this image carries "
                         + ", ".join(sorted(bundles)))
    # Nothing to go on yet — any of them describes the release equally well.
    return bundles[sorted(bundles)[0]]


def bundle_summary():
    bundles = bundle_chips()
    fw = sorted({m["fw"] for m in bundles.values()})
    return f"{'/'.join(fw)} for {', '.join(sorted(bundles))}"


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


def _as_address(text):
    """An address object, or None — the firmware writes fe80:0000:..., `ip`
    writes fe80::..., and only the parsed form compares reliably."""
    try:
        return ipaddress.ip_address(str(text).strip())
    except (ValueError, TypeError):
        return None


def _via_address(route_line):
    parts = route_line.split()
    if "via" in parts:
        try:
            return _as_address(parts[parts.index("via") + 1])
        except IndexError:
            return None
    return None


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
        # The prefix is unchanged — but the border router behind it may not be.
        # Replace a stick and the network keeps its prefix while the next hop
        # becomes the new stick's link-local; a route still pointing at the old
        # one is a black hole that looks perfectly healthy in `ip -6 route`.
        # Measured: after a swap the mesh answered nothing until this was
        # repointed by hand.
        if "proto static" in route:
            have, want = _via_address(route), _as_address(ll)
            if want and have != want:
                rc, _ = run_ip(["-6", "route", "replace", omr, "via", ll,
                                "dev", tap, "proto", "static",
                                "metric", "1024"], check=True)
                if rc == 0:
                    log(f"{omr} pointed at {have}, which no longer answers — "
                        f"repointed to {ll}, the border router now on this port")
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

def matter_greeting(host, port, timeout=4.0):
    """What answers on this address, in its own words.

    A Matter server greets every new websocket client before being asked
    anything, and that greeting says which implementation it is and whether it
    accepts a proxy radio.  Both matter here: a server without the proxy looks
    exactly like a working one until the first commissioning fails.  Returns
    the greeting, or None when nothing Matter-shaped answers.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as c:
            c.settimeout(timeout)
            key = base64.b64encode(os.urandom(16)).decode()
            c.sendall((f"GET /ws HTTP/1.1\r\nHost: {host}:{port}\r\n"
                       "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                       f"Sec-WebSocket-Key: {key}\r\n"
                       "Sec-WebSocket-Version: 13\r\n\r\n").encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = c.recv(4096)
                if not chunk:
                    return None
                buf += chunk
            if b" 101 " not in buf.split(b"\r\n", 1)[0]:
                return None
            buf = buf.partition(b"\r\n\r\n")[2]
            # One unmasked text frame from the server: enough for the greeting.
            while len(buf) < 2:
                buf += c.recv(4096)
            ln, off = buf[1] & 0x7F, 2
            if ln == 126:
                while len(buf) < 4:
                    buf += c.recv(4096)
                ln, off = struct.unpack(">H", buf[2:4])[0], 4
            elif ln == 127:
                while len(buf) < 10:
                    buf += c.recv(4096)
                ln, off = struct.unpack(">Q", buf[2:10])[0], 10
            while len(buf) < off + ln:
                buf += c.recv(65536)
            return json.loads(buf[off:off + ln].decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None


def matter_relay():
    """Let the stick reach the Matter server on the host's loopback interface.

    The BLE proxy in the firmware is a websocket client: it dials a Matter
    server and offers it a radio.  Under Home Assistant that server is another
    add-on, and its port is published on loopback only — an address the stick
    cannot reach, sitting a hop away on the tap.  Both ends are on this
    machine, so the process that owns the tap listens on its host side and
    carries the bytes across.  Nothing here understands websockets or Matter;
    it is a pipe.

    Off when THBR_MATTER_ADDR is empty.  Nothing is dialled until the stick
    connects, so a missing or restarted Matter server costs one refused
    connection and nothing else.
    """
    target = ENV["matter_addr"].strip()
    if not target:
        return
    host, _, port = target.rpartition(":")
    if not host or not port.isdigit():
        log(f"THBR_MATTER_ADDR={target} is not host:port — forwarder off")
        return
    dest = (host, int(port))
    host_ip = ENV["host_addr"].split("/")[0]

    port = ENV["matter_port"]
    if dest == (host_ip, port):
        log(f"THBR_MATTER_ADDR points at this forwarder's own address "
            f"{host_ip}:{port} — that would be a loop; forwarder off")
        return

    # The tap address and a Matter server's wildcard bind are the same port, so
    # whichever of the two starts first locks the other out — SO_REUSEADDR does
    # not share a listening port.  So do not take the port on spec: wait until
    # the server this would forward TO actually answers.  Then either it is on
    # loopback only and the tap address is free for us, or it holds every
    # interface — in which case the bind below fails, and it should, because
    # the stick can reach that server without any help from here.  Taking the
    # port first and sorting it out afterwards is what breaks a Matter server
    # that restarts later: its wildcard bind then fails for good (errno 98).
    said_waiting = False
    while matter_greeting(dest[0], dest[1]) is None:
        if not said_waiting:
            said_waiting = True
            log(f"waiting for a Matter server on {dest[0]}:{dest[1]} before "
                f"offering the stick a way to it — nothing to forward until then")
        time.sleep(FORWARDER_PROBE_S)

    srv = None
    waited = 0.0
    while srv is None:
        try:
            t = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            t.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            t.bind((host_ip, port))
            t.listen(4)
            srv = t
        except OSError as e:
            if e.errno != errno.EADDRINUSE:
                time.sleep(2.0)          # tap not up yet; it appears within seconds
                continue
            # Somebody is already on the address the stick dials.  That is not
            # necessarily wrong: a Matter server run as an ordinary container
            # usually listens on every interface, which covers this one, and
            # then the stick reaches it without help.  Which of the two it is
            # decides whether anything is broken, so look rather than guess.
            greeting = matter_greeting(host_ip, port)
            if greeting is not None:
                if greeting.get("ble_proxy_enabled"):
                    log(f"a Matter server answers on {host_ip}:{port} "
                        f"({greeting.get('sdk_version', 'unknown build')}) and "
                        f"takes a proxy radio — the stick reaches it directly, "
                        f"no forwarding needed")
                else:
                    log(f"a Matter server answers on {host_ip}:{port} "
                        f"({greeting.get('sdk_version', 'unknown build')}), but "
                        f"it reports no BLE proxy.")
                    log("    The stick will offer its radio and be turned away. "
                        "Commissioning over Bluetooth needs a server that "
                        "accepts a proxy radio, with that option switched on.")
                # Stand down, and stay down.  Retrying this bind would take the
                # port the moment that server restarts, and its wildcard bind
                # would then fail for good with errno 98 — a border router
                # holding a Matter server down, which is the wrong way round.
                log(f"    Leaving {host_ip}:{port} to it and not retrying, so it "
                    "still has the port after a restart.")
                return
            # Not a Matter server.  Most likely a process on its way out, so
            # wait a little rather than claim the port from under it — but not
            # forever, and never by surprise.
            if waited >= 50.0:
                log(f"NOT forwarding: something already listens on "
                    f"{host_ip}:{port} and it does not answer as a Matter "
                    f"server.")
                log("    That is the address the stick dials, so it reaches "
                    "that instead and reports no Matter server. Move the "
                    "other service, or set THBR_MATTER_PORT.")
                return
            time.sleep(10.0)
            waited += 10.0
    log(f"BLE proxy forwarder: {host_ip}:{port} -> {dest[0]}:{dest[1]}")

    def pump(src, dst, who, state):
        err = None
        try:
            while True:
                chunk = src.recv(8192)
                if not chunk:
                    break
                dst.sendall(chunk)
        except OSError as e:
            err = e
        finally:
            # Whoever gets here first is the side that hung up.  A link that
            # dies young is worth a line: it is the difference between "no
            # Matter server" and "something keeps closing this".
            if state["ended"] is None:
                state["ended"] = (who, err, time.time() - state["start"])
                w, e, secs = state["ended"]
                if secs < 60:
                    log(f"BLE proxy link closed by the {w} after {secs:.1f}s"
                        + (f" ({e})" if e else ""))
            for sk in (src, dst):
                try: sk.shutdown(socket.SHUT_RDWR)
                except OSError: pass

    complained = [0.0]
    while True:
        try:
            client, _ = srv.accept()
        except OSError:
            time.sleep(1.0)
            continue
        try:
            upstream = socket.create_connection(dest, timeout=5)
            # The dial timeout must not outlive the dial.  Left in place it
            # applies to every recv as well, and this link is idle for minutes
            # at a time — the connection died five seconds after the handshake,
            # every time, and looked like the far end hanging up.
            upstream.settimeout(None)
            client.settimeout(None)
        except OSError as e:
            client.close()
            now = time.time()
            if now - complained[0] > 60:      # once a minute is plenty
                complained[0] = now
                log(f"the stick asked for the Matter server at {dest[0]}:{dest[1]} "
                    f"and it did not answer ({e}) — is the Matter server add-on "
                    f"running with its BLE proxy enabled?")
            continue
        state = {"start": time.time(), "ended": None}
        for a, b, who in ((client, upstream, "stick"),
                          (upstream, client, "Matter server")):
            threading.Thread(target=pump, args=(a, b, who, state),
                             daemon=True).start()


# Tags the border router's own web server logs under while it collects
# diagnostics.  Measured on a live installation: about fifty lines a minute,
# steady, all of it routine -- enough to push this add-on's own lines out of a
# rotated `docker logs` within minutes.  Warnings and errors from these tags
# are still kept; only the running commentary goes.
STICK_QUIET_TAGS = ("web_base", "obtr_web")
STICK_LINE = re.compile(r"^([VDIWE]) \(\d+\) ([A-Za-z0-9_.\-]+):")


def stick_line_wanted(line, mode):
    """Whether a line from the stick is worth repeating on our stdout."""
    if mode == "off":
        return False
    if mode != "quiet":
        return True
    m = STICK_LINE.match(line.strip())
    return not (m and m.group(1) in "VDI" and m.group(2) in STICK_QUIET_TAGS)


def log_relay():
    """Print the stick's UDP log lines on our stdout, so `docker logs` has them.

    Filtered by THBR_STICK_LOG.  The full stream is always on the stick's own
    console; what this decides is only how much of it lands in the container
    log, where it competes with the lines this add-on writes about itself.
    """
    mode = ENV["stick_log"]
    if mode not in ("quiet", "all", "off"):
        log(f"THBR_STICK_LOG={mode} is not quiet, all or off — using quiet")
        mode = "quiet"
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
            if tail.strip() and stick_line_wanted(tail, mode):
                print("[stick]", tail.rstrip(), flush=True)
            tail = ""
            continue
        text = tail + data.decode("utf-8", "replace")
        lines = text.split("\n")
        tail = lines.pop()
        for line in lines:
            if line.strip() and stick_line_wanted(line, mode):
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
        return None

    if chip != normalise_chip(m["chip"]):
        alt = bundle_chips().get(chip)
        if alt is None:
            log(f"NOT flashing: {port} holds an {chip}, and this image carries "
                f"firmware for {', '.join(sorted(bundle_chips())) or m['chip']}.")
            log("    Either the wrong device is configured, or this board is not "
                "one this firmware fits.")
            return None
        m = alt
        app_offset, our_project, our_version = bundled_app(m)
        log(f"{port} holds an {chip}; taking the firmware bundled for it")

    named = mac_from_port(port)
    if named and mac and named != mac:
        log(f"NOT flashing: {port} is named after {named} but the chip answers "
            f"{mac} — the path points somewhere else than it claims.")
        return None

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
            return None
        log(f"forced: replacing the application '{project}' on {port}")

    known = f"target confirmed: {chip}"
    if mac:
        known += f", MAC {mac}"
    if project:
        known += f", carrying '{project}'" + (f" {version}" if version else "")
    log(known)
    return m


def flash(m, force=False):
    """Write all bundled images.  The port must be free (pump stopped)."""
    verify_bundle(m)
    m = check_target(m, force)
    if m is None:
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
    # No --chip: this reads a raw flash region, which needs the chip detected,
    # not asserted.  Naming one turns a settings file into something that can
    # only be read back by the same kind of chip it came from — and esptool
    # refuses outright when the two differ, which is what a replacement stick
    # of another family runs into.
    cmd = [sys.executable, "-m", "esptool",
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
    # No --chip, for the reason given in backup_nvs: the settings are the same
    # bytes whatever carries them, and asserting a chip here is what stops a
    # network from moving to a replacement stick of a different family.
    cmd = [sys.executable, "-m", "esptool",
           "--port", ENV["device"], "--before", "default-reset",
           "--after", "hard-reset", "write-flash", "0x9000", path]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    ok = proc.returncode == 0 and "Hash of data verified" in proc.stdout
    log(f"restored the network settings from {os.path.basename(path)}: "
        f"{'OK' if ok else 'FAILED'}")
    if not ok:
        log("    " + ((proc.stderr or proc.stdout).strip().splitlines() or ["no output"])[-1][:160])
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
    log(f"THBR {ENV['release'] or 'dev'}; bundled firmware {bundle_summary()}; "
        f"policy {ENV['policy']}")
    adopt_port()
    os.makedirs(RUN_DIR, exist_ok=True)
    for f in (REQ_FILE, RES_FILE):
        if os.path.exists(f):
            os.unlink(f)
    with open(PID_FILE, "w") as fh:
        fh.write(str(os.getpid()))

    threading.Thread(target=log_relay, daemon=True).start()
    threading.Thread(target=matter_relay, daemon=True).start()

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
        # Now that the chip is known, work from its bundle.  Until here any of
        # them would do; from here the wrong one would refuse the stick as the
        # wrong chip and quietly decline to flash it.
        if info.get("chip"):
            alt = bundle_chips().get(normalise_chip(info["chip"]))
            if alt is not None:
                m = alt
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
    print(f"bundled:   THBR {bundle_summary()}")
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
