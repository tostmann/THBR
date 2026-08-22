#!/usr/bin/env python3
"""Drive an OpenThread CLI node over its serial port.

Used to join a second 802.15.4 device to the border router's mesh, which is
what turns "the host can reach the BR's own address" into "the host can reach
the mesh".

Usage:
    ot_cli.py <device> <command> [<command> ...]
    ot_cli.py <device> --wait-attached [timeout_s]
"""

import os
import stat
import sys
import time

import serial


def send(ser, cmd, settle=0.6):
    ser.reset_input_buffer()
    ser.write((cmd + "\n").encode())
    ser.flush()
    time.sleep(settle)
    out = ser.read(ser.in_waiting or 1).decode("utf-8", "replace")
    # Echo of the command itself is noise.
    return "\n".join(l for l in out.splitlines() if l.strip() and l.strip() != cmd)


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    dev = sys.argv[1]
    if not stat.S_ISCHR(os.stat(os.path.realpath(dev)).st_mode):
        sys.exit(f"{dev} is not a character device")

    ser = serial.Serial(dev, 115200, timeout=1.0)
    time.sleep(0.3)

    if sys.argv[2] == "--wait-attached":
        timeout = float(sys.argv[3]) if len(sys.argv) > 3 else 120.0
        end = time.time() + timeout
        last = ""
        while time.time() < end:
            # The ESP console hosts the OpenThread CLI as a sub-command when
            # CONFIG_OPENTHREAD_CONSOLE_ENABLE=n, which is the example default.
            state = send(ser, "ot state", 0.4)
            last = state.replace("Done", "").strip()
            if last in ("child", "router", "leader"):
                print(f"attached as {last}")
                return 0
            time.sleep(2)
        print(f"not attached after {timeout:.0f}s (state={last})")
        return 1

    for cmd in sys.argv[2:]:
        print(f"> {cmd}")
        print(send(ser, cmd))
    return 0


if __name__ == "__main__":
    sys.exit(main())
