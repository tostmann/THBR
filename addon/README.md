# THBR — Thread border router on a USB stick

Two ways to run the host side, built from this one directory:

- **Home Assistant add-on** — add this repository under *Settings → Apps →
  App store → ⋮ → Repositories*, then install **THBR Thread Border Router**.
  The Supervisor pulls the published image; nothing is built on your machine.
  Step by step in the [repository README](../README.md), add-on specific
  documentation in [DOCS.md](DOCS.md).
- **Plain Docker**, next to Home Assistant in Docker — described below.

## Plain Docker

One container, one ESP32-C6 stick, no extra network hardware: the stick runs
an OpenThread border router and reaches the host through its USB port, which
the container turns into a network interface (`tap0`). Home Assistant then
discovers the router over mDNS, imports the Thread network and commissions
Matter-over-Thread devices, exactly as with any other border router.

The container also carries the stick firmware and flashes it over the same USB
port, so a new stick needs nothing but being plugged in.

## Requirements

- An ESP32-C6 board with its native USB port (USB-Serial/JTAG) connected to
  the Docker host. 4 MB flash.
- Home Assistant Core in Docker with `network_mode: host`.
- The Matter integration (python-matter-server) if you want Matter devices;
  it needs no THBR-specific settings.

## Quick start

1. Find the stick's stable device path:
   ```
   ls /dev/serial/by-id/ | grep Espressif
   ```
2. Put the `thbr` service from `compose.yaml` next to your Home Assistant
   service, set `THBR_DEVICE` to that path, and give Home Assistant the
   `depends_on` shown there. Start it:
   ```
   docker compose up -d thbr
   docker logs -f thbr
   ```
   The image comes from Docker Hub (`tostmann/thbr`, arm64 and amd64). To run
   your own instead, build it with `docker build -t thbr addon/` from the
   repository root and name that in the compose file.
   A new stick is flashed on first start (about a minute); the stick's own log
   lines appear prefixed `[stick]`. The container is healthy once the stick
   answers through the backbone.
3. Tell Home Assistant to use the tap: *Settings → System → Network → Network
   adapter*, enable your LAN adapter **and** `tap0`, then restart Home
   Assistant. Without this HA's mDNS never listens on the tap and the router is
   not found.
4. *Settings → Devices & services → Add integration → Open Thread Border
   Router*, and give it `http://192.168.45.2` (`THBR_STICK_ADDR`, if you
   changed it). HA then imports the network's dataset from the router's REST
   API. It is typed in rather than offered: the router does announce itself
   over mDNS, but that discovery feeds the *Thread* integration; the *Open
   Thread Border Router* integration has none except for Home Assistant's own
   border-router add-on.

## Choosing the right port

The `THBR_DEVICE` setting takes every serial port on the machine, and on a system with
a few USB devices the wrong path is easy to write. Three things keep that from
mattering.

**The port names the chip.** THBR runs on the C6's own USB port, which
enumerates as `usb-Espressif_USB_JTAG_serial_debug_unit_<MAC>-if00`. That MAC
is the chip's, so the name identifies one specific device and keeps doing so
when `ttyACM` numbers move around after a replug. The add-on says at startup
which chip it adopted, and says it plainly when the configured port is not one of
these. If the port was given as a bare `/dev/ttyACM3` and that number moves,
the add-on looks the same chip up again by its MAC.

**Nothing is written before the chip has been asked what it is.** Ahead of any
flash the add-on reads the chip type and MAC over the ROM protocol and refuses
unless both agree: the chip has to be the one the bundled firmware is for, and
the one the port is named after. Asking costs the device a reset — there is no
way to ask a chip anything without one — and it is reset straight back
afterwards, so a device that turns out to be something else is left as it was
found. What that looks like in the log:

**And the application is asked for its name.** A chip type is not an identity:
another ESP32-C6 board on the same machine — a different stick, an ESPHome node
— is the same chip on the same kind of port, and neither the port name nor the
chip type tells it apart from this stick. What does is the name its application
was built under, which every ESP-IDF application carries in its image. The
add-on reads it and writes nothing over an application it did not build itself.
Converting such a board is possible, but it has to be asked for: `thbrctl flash
--force`, or the update button on the add-on's page.

```
target confirmed: esp32c6, MAC xx:xx:xx:xx:xx:xx, carrying 'thbr' 0.1
NOT flashing: /dev/… holds an esp32h2, the bundled firmware is for esp32c6.
NOT flashing: /dev/… carries the application 'culfw32' 2.1, not 'thbr'.
NOT flashing: nothing on /dev/… answers as an Espressif chip — …
```

There is no dmesg to fall back on — `/dev/kmsg` cannot be read from inside a
container. What there is: udev creates the by-id link when a device appears, so
its timestamp is when that device was last plugged in. With no device
configured yet, the add-on lists the candidates newest first and says how long
ago each one appeared, which makes the shortest way to the right port plugging
the stick in last, deliberately, just before choosing it.

One caveat that follows from how it works: after a reboot every port was
created within the same second, so the order carries no information at all.
Unplugging the stick and plugging it back in makes it the newest again — worth
doing before picking it out of the list on a machine with a few USB devices.

esptool refuses a mismatched image on its own as well, but only once it has
connected, phrased as a problem with the image header, and only for chips it
recognises — an unknown one it warns about and carries on past. Asking first
answers the question in one line, before anything is written.

## Commands

```
docker exec thbr python3 /opt/thbr/thbrctl.py version   # bundled vs installed
docker exec thbr python3 /opt/thbr/thbrctl.py flash     # reflash the bundled firmware
docker exec thbr python3 /opt/thbr/thbrctl.py flash --force
```

Leaving the device unset makes the container list the ports it can see instead
of starting, which is the quickest way to find the right `/dev/serial/by-id/…`
path.

Flashing stops the backbone for about a minute; border routing and the
Thread network itself resume from the stick's flash — no device has to be
re-commissioned. Only `esptool erase-flash` (never run by this container)
destroys the Thread network, and since firmware 0.1.44 that is final: an
erased stick does not return to a known network, it generates a new random
one, and every device paired on the old network stays behind. Save the
network data first — from the add-on page, or `GET /node/dataset/active` —
because that saved copy is the only way back.

## Flash policy (`THBR_FLASH`)

| value     | behaviour |
|-----------|-----------|
| `auto`    | default — flash only a stick that answers nothing at all (new stick, RCP image) |
| `upgrade` | additionally replace an older THBR or a foreign OTBR firmware when the image carries a different version |
| `never`   | never write the flash |

There is no OTA on the stick and a running border router is never reflashed
unasked.

## Who can reach the web interface

Under plain Docker, everyone this host is reachable by. There is no ingress to
authenticate against, so the container leaves its port open and says so at
startup — and that page can flash the stick, restart it and hand out the Thread
credentials. Put it behind something, or name who may reach it:

```
THBR_WEB_ALLOW: "192.168.1.0/24"     # a network you trust
THBR_WEB_ALLOW: "127.0.0.1"          # nothing but this host
THBR_WEB_ALLOW: "any"                # the default here, stated explicitly
```

As a Home Assistant add-on the default is the opposite way round: only the
Supervisor's own network is answered, because ingress authenticates and the raw
port does not.

## The stick's network

The backbone is a private point-to-point link: host `192.168.45.1/24`,
stick `192.168.45.2`. The stick's ot-br-posix-compatible REST API answers on
`http://192.168.45.2/` (port 80; `/node`, `/node/dataset/active`,
`/diagnostics`, …) and its firmware info on `http://192.168.45.2:8082/version`
and `/status`. Change the addresses with `THBR_HOST_ADDR` / `THBR_STICK_ADDR`
only if the subnet collides with something on your host (the stick side is a
firmware build option).

## Troubleshooting

- **`waiting for /dev/serial/by-id/...`** — the path is wrong or `/dev` is not
  mapped into the container. The compose example maps the live `/dev` on
  purpose: a flashed stick re-enumerates and may change its `ttyACM` number.
- **Healthy, but HA does not discover the router** — step 3 above: `tap0` is
  not among HA's network adapters, or HA started before the tap existed.
- **`WARNING tap0 sysctls not set`** in the log — the `/proc/sys/net/ipv6/conf`
  volume from `compose.yaml` is missing. Without `accept_ra=2` and
  `accept_ra_rt_info_max_plen=64` on the tap the host ignores the router's
  route advertisements and cannot reach Thread devices.
- **`br=stopped` in the `[stick]` log for minutes** — the firmware probes and
  repairs the interface index itself; if it persists, open an issue with
  `docker logs thbr`. From add-on 2026.8.35 the log is safe to share: it
  blanks out the network credentials older firmware prints at boot.
- **Matter devices unavailable after switching from another border router** —
  the matter-server container may pin `--primary-interface` to the LAN; it must
  be `tap0` or unset.

## The route into the mesh

The host reaches Thread devices through a route the border router advertises.
The kernel only accepts such a route when two per-interface options are set,
and inside a container `/proc/sys` is read-only — hence the
`/proc/sys/net/ipv6/conf` volume in the compose file, which the Home Assistant
add-on cannot have. Where the options cannot be set, the container installs the
route (and, if the kernel did not autoconfigure one, an address) itself and
says so in the log; as soon as the kernel does accept the advertisement, the
hand-installed route is withdrawn again in favour of the advertised one, which
carries the border router's lifetimes.

## Building it yourself

```
THBR_STAGE=2 scripts/build.sh      # firmware (ESP-IDF, see scripts/idf_env.sh)
scripts/dist.sh                    # copies the flash images into addon/firmware/
docker build -t thbr addon/        # plain Docker image
```

The published add-on images are built from this same directory, one per
architecture, by `scripts/publish_images.sh`; `build.yaml` names the base image
each is built from. Deleting the `image:` line in `config.yaml` makes the
Supervisor build the directory on the user's machine instead, which is the way
to test a change before publishing it.

## Licence

The project is published under the **PolyForm Noncommercial License 1.0.0**:
use it freely for anything that is not commercial. The full text is in
[LICENSE](../LICENSE) at the root of the repository.

The components it builds on keep their own licences and are not narrowed by
that choice — OpenThread under BSD-3-Clause, the Espressif components under
Apache-2.0, cJSON under MIT, and esptool, which the image ships unmodified and
runs as a separate program, under GPL-2.0-or-later. [NOTICE](../NOTICE) lists
them.
