#!/usr/bin/env python3
"""Bridge a Linux TAP device to a busware stick over its CDC-ACM port.

The stick has no USB device controller other than the fixed-function
USB-Serial/JTAG block (ESP32-C6 TRM ch. 32), so a CDC-ECM/NCM Ethernet gadget
is impossible: Ethernet frames have to travel through the serial byte stream.
This pump does that, with SLIP framing (RFC 1055) over the ACM link and a TAP
device on the Linux side.

Why TAP and not PPP: the OpenThread border-router library only initialises its
infrastructure-interface side for a netif with a MAC and a broadcast domain.
Measured 2026-08-20 on this hardware — over PPP, border_router_init() returns
ESP_OK and Thread reaches leader, but the routing manager stays 'stopped' and
otPlatInfraIfStateChanged answers INVALID_STATE.  See THBR/PLAN.md.

Framing: SLIP, not HDLC.  USB already guarantees integrity per packet (CRC +
retransmission), so PPP's FCS would be redundant; what remains is finding
frame boundaries after a buffer overrun, which the END byte does.

Usage:
    ./tap_pump.py --dev /dev/serial/by-id/usb-Espressif_... [--tap tap0]
                  [--addr 192.168.45.1/24] [--mtu 1500]

Requires CAP_NET_ADMIN (run as root, or in an add-on container with NET_ADMIN
and /dev/net/tun mapped — the same privileges the official Home Assistant
OpenThread Border Router add-on declares).
"""

import argparse
import errno
import fcntl
import os
import select
import struct
import subprocess
import sys
import termios
import time

# <linux/if_tun.h>
TUNSETIFF = 0x400454CA
TIOCMBIS, TIOCMBIC = 0x5416, 0x5417
TIOCM_DTR, TIOCM_RTS = 0x002, 0x004
TUNSETPERSIST = 0x400454CB
IFF_TAP = 0x0002
IFF_NO_PI = 0x1000

# RFC 1055
SLIP_END = 0xC0
SLIP_ESC = 0xDB
SLIP_ESC_END = 0xDC
SLIP_ESC_ESC = 0xDD


def slip_encode(frame: bytes) -> bytes:
    out = bytearray([SLIP_END])
    for b in frame:
        if b == SLIP_END:
            out += bytes([SLIP_ESC, SLIP_ESC_END])
        elif b == SLIP_ESC:
            out += bytes([SLIP_ESC, SLIP_ESC_ESC])
        else:
            out.append(b)
    out.append(SLIP_END)
    return bytes(out)


class SlipDecoder:
    """Incremental decoder; yields complete frames as they finish."""

    def __init__(self, max_frame: int):
        self.buf = bytearray()
        self.esc = False
        self.max_frame = max_frame
        self.overruns = 0

    def feed(self, data: bytes):
        for b in data:
            if b == SLIP_END:
                if self.buf:
                    frame = bytes(self.buf)
                    self.buf.clear()
                    self.esc = False
                    yield frame
                continue
            if self.esc:
                if b == SLIP_ESC_END:
                    self.buf.append(SLIP_END)
                elif b == SLIP_ESC_ESC:
                    self.buf.append(SLIP_ESC)
                else:
                    # Protocol violation: drop the partial frame rather than
                    # hand lwIP something malformed.
                    self.buf.clear()
                self.esc = False
                continue
            if b == SLIP_ESC:
                self.esc = True
                continue
            if len(self.buf) >= self.max_frame:
                self.overruns += 1
                self.buf.clear()
                self.esc = False
                continue
            self.buf.append(b)


def open_tap(name: str, persist: bool = False) -> tuple[int, str]:
    fd = os.open("/dev/net/tun", os.O_RDWR)
    ifr = struct.pack("16sH", name.encode(), IFF_TAP | IFF_NO_PI)
    res = fcntl.ioctl(fd, TUNSETIFF, ifr)
    real = res[:16].rstrip(b"\0").decode()
    if persist:
        # Keep the interface when this process dies.  Home Assistant binds its
        # zeroconf sockets per interface at start-up; a tap that vanishes with
        # the pump and comes back under the same name is a NEW interface to
        # it, and Thread discovery stays dark until HA restarts.  A persistent
        # tap survives pump restarts (stick re-enumeration, firmware flash).
        fcntl.ioctl(fd, TUNSETPERSIST, 1)
    return fd, real


def open_serial(path: str) -> int:
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
    if not os.isatty(fd):
        raise SystemExit(f"{path} is not a tty")
    # Raw mode.  Baud rate is meaningless on CDC-ACM but the line discipline
    # would otherwise mangle 0x0d/0x11/0x13 and echo everything back.
    attrs = termios.tcgetattr(fd)
    attrs[0] = 0                                   # iflag
    attrs[1] = 0                                   # oflag
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    attrs[3] = 0                                   # lflag: no ICANON/ECHO/ISIG
    attrs[6][termios.VMIN] = 1
    attrs[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    # On the C6 the USB-Serial/JTAG block maps DTR/RTS onto the reset and boot
    # pins, which is how esptool enters the ROM loader.  Opening the port can
    # leave them asserted, and a chip that lands in the ROM loader says nothing
    # at all — the failure looks exactly like a dead cable.  Clear both.
    fcntl.ioctl(fd, TIOCMBIC, struct.pack("I", TIOCM_DTR | TIOCM_RTS))
    return fd


def hard_reset(fd):
    """Pulse the reset line so a chip stuck in the ROM loader runs its app."""
    fcntl.ioctl(fd, TIOCMBIC, struct.pack("I", TIOCM_DTR))   # IO0 high = normal boot
    time.sleep(0.1)
    fcntl.ioctl(fd, TIOCMBIS, struct.pack("I", TIOCM_RTS))   # EN low  = in reset
    time.sleep(0.25)
    fcntl.ioctl(fd, TIOCMBIC, struct.pack("I", TIOCM_RTS))   # released -> boots


def wait_for_traffic(fd, quiet_s: float = 25.0):
    """The firmware talks every few seconds.  Silence after opening the port
    means the chip is not running its application — reset it once and see."""
    deadline = time.time() + quiet_s
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 1.0)
        if r:
            return True
    print(f"[pump] no traffic for {quiet_s:.0f}s — resetting the stick", flush=True)
    hard_reset(fd)
    deadline = time.time() + 15.0
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 1.0)
        if r:
            print("[pump] stick started talking after the reset", flush=True)
            return True
    print("[pump] still silent after a reset — check the hardware", flush=True)
    return False


# Errors that mean "this descriptor is finished": the device was unplugged, or
# it re-enumerated and the kernel handed the old node to nobody.  A USB reset of
# the stick produces exactly this, and the port comes back under a possibly
# different name — which is why the by-id path is reopened rather than the fd
# retried.
GONE = (errno.EIO, errno.ENXIO, errno.ENODEV, errno.EBADF, errno.EPIPE)


def reopen_serial(old_fd, path: str, settle: float = 0.5):
    """Wait for the stick to come back and open it again."""
    try:
        os.close(old_fd)
    except OSError:
        pass
    print(f"[pump] {path} went away — waiting for it to come back", flush=True)
    waited = 0.0
    while True:
        try:
            if os.path.exists(path):
                time.sleep(settle)          # let udev finish with the new node
                fd = open_serial(path)
                print(f"[pump] {path} is back after {waited:.0f}s, link resumed", flush=True)
                wait_for_traffic(fd)
                return fd
        except OSError as e:
            print(f"[pump] reopen failed ({e}), retrying", flush=True)
        time.sleep(1.0)
        waited += 1.0


def run(cmd: list[str]):
    subprocess.run(cmd, check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", required=True, help="stick CDC-ACM port")
    ap.add_argument("--tap", default="tap0")
    ap.add_argument("--addr", default="", help="IPv4 CIDR for the tap, e.g. 192.168.45.1/24")
    ap.add_argument("--mtu", type=int, default=1500)
    ap.add_argument("--stats", type=float, default=0.0, help="print counters every N s")
    ap.add_argument("--persist", action="store_true",
                    help="keep the tap when the pump exits (survives restarts)")
    ap.add_argument("--sysctl-dir", default="",
                    help="fallback path of the host's /proc/sys/net/ipv6/conf (container)")
    args = ap.parse_args()

    max_frame = args.mtu + 14 + 4     # payload + Ethernet header + slack

    tap_fd, tap_name = open_tap(args.tap, args.persist)
    ser_fd = open_serial(args.dev)
    wait_for_traffic(ser_fd)

    # A freshly created tap starts with kernel defaults, and two of them break
    # border routing silently:
    #   accept_ra=1 is ignored once forwarding is on -> must be 2
    #   accept_ra_rt_info_max_plen=0 discards the BR's /64 route information,
    #     so the route into the Thread mesh never appears
    # Setting them here means the link works right after the pump starts,
    # instead of after someone remembers the sysctls.
    #
    # Inside a container /proc/sys is read-only; the host's conf tree can be
    # bind-mounted elsewhere (--sysctl-dir, e.g. /hostsys) and is tried next.
    for key, val in (("accept_ra", "2"), ("accept_ra_rt_info_max_plen", "64")):
        last = None
        for base in ("/proc/sys/net/ipv6/conf", args.sysctl_dir):
            if not base:
                continue
            try:
                with open(f"{base}/{tap_name}/{key}", "w") as f:
                    f.write(val)
                last = None
                break
            except OSError as e:
                last = e
        if last is not None:
            print(f"[pump] could not set {key}={val} on {tap_name}: {last} — "
                  f"without it the host will not learn the route into the mesh",
                  flush=True)

    # Link up only now: the kernel sends its router solicitations the moment
    # the interface comes up, and the advertisement that answers must meet
    # the sysctls already in place.
    run(["ip", "link", "set", tap_name, "mtu", str(args.mtu)])
    run(["ip", "link", "set", tap_name, "up"])
    if args.addr:
        run(["ip", "addr", "replace", args.addr, "dev", tap_name])

    print(f"{tap_name} <-> {args.dev} (mtu {args.mtu})", flush=True)

    dec = SlipDecoder(max_frame)
    n_to_stick = n_from_stick = n_dropped = 0
    next_stats = time.time() + args.stats if args.stats else None

    while True:
        timeout = 1.0 if next_stats else None
        r, _, _ = select.select([tap_fd, ser_fd], [], [], timeout)

        if tap_fd in r:
            frame = os.read(tap_fd, max_frame)
            if frame:
                try:
                    os.write(ser_fd, slip_encode(frame))
                    n_to_stick += 1
                except OSError as e:
                    # Stick unplugged or re-enumerating: drop the frame, the
                    # way a cable would.  Dying here would take the interface
                    # down with us.
                    n_dropped += 1
                    if e.errno in GONE:
                        ser_fd = reopen_serial(ser_fd, args.dev)
                        dec = SlipDecoder(max_frame)
                        continue
                    if e.errno not in (errno.EAGAIN,):
                        raise

        if ser_fd in r:
            try:
                data = os.read(ser_fd, 4096)
            except OSError as e:
                if e.errno in GONE:
                    ser_fd = reopen_serial(ser_fd, args.dev)
                    dec = SlipDecoder(max_frame)
                    continue
                raise
            if not data:
                # EOF on a character device means the far side went away — the
                # port has to be opened again, the old descriptor never heals.
                ser_fd = reopen_serial(ser_fd, args.dev)
                dec = SlipDecoder(max_frame)
                continue
            for frame in dec.feed(data):
                # Anything shorter than an Ethernet header is noise — most
                # likely the firmware's plain-text boot banner before the
                # handover, which shares this port.
                if len(frame) >= 14:
                    try:
                        os.write(tap_fd, frame)
                        n_from_stick += 1
                    except OSError as e:
                        # The tap being administratively down is not fatal
                        # either — an interface coming back up must find the
                        # pump still running.
                        n_dropped += 1
                        if e.errno not in (errno.EIO, errno.ENETDOWN, errno.EAGAIN):
                            raise

        if next_stats and time.time() >= next_stats:
            print(f"[pump] to_stick={n_to_stick} from_stick={n_from_stick} "
                  f"overruns={dec.overruns} dropped={n_dropped}", flush=True)
            next_stats = time.time() + args.stats


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
