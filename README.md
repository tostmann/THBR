# THBR — a Thread border router on a stick

The whole border router runs on an ESP32-C6. It reaches the host through the
chip's own USB port, which the host side turns into a network interface. There
is no radio co-processor on the host, no Ethernet chip on the stick, and no
Spinel link that stops working when a host process restarts.

Because the border router lives on the stick, the Thread network survives what
happens on the host: restarting Home Assistant, updating the add-on, recreating
the container. The mesh keeps running; only the path to it pauses.

Runs as a **Home Assistant add-on** or as a **plain Docker container** next to
Home Assistant. Both are built from the same directory.

![The add-on's page in Home Assistant: state of stick and network, and the mesh topology](images/haos.png)

## What it does

- **Border routing** — the stick forms or joins a Thread network, advertises a
  route to it, and the host reaches Thread devices over IPv6.
- **Matter over Thread** — Home Assistant discovers the router, imports the
  network, and commissions devices through it. Firmware updates for those
  devices flow through it too.
- **A page in the sidebar** — firmware on the stick against the one the add-on
  carries, border routing state, memory, the route into the mesh and whether
  anything answers through it, plus a graph of the mesh you can pull around.
- **Two buttons that used to need a shell** — write the bundled firmware to the
  stick over its USB port, and restart the stick.
- **Save the network settings** — the stick's Thread credentials, in a file
  Home Assistant backups carry. Restoring it onto a replacement stick moves the
  network to new hardware without re-commissioning a single device.

## Requirements

- An ESP32-C6 with 4 MB flash, connected by its **native USB port**
  (USB-Serial/JTAG). Other Espressif chips with a border-router library exist,
  but the ESP32-H2 is not among them — Espressif ships none for it.
- Home Assistant OS or Supervised, or Home Assistant in Docker on a host you
  control.

## Install on Home Assistant

Home Assistant OS or Supervised. The Supervisor pulls a published image, so
this takes seconds rather than a build on your machine.

1. **Add this repository.** *Settings → Apps → App store → ⋮ → Repositories*,
   paste `https://github.com/tostmann/THBR`, **Add**. (Older versions call
   these Add-ons, as do the Supervisor's API and this project's own files.)

   The Supervisor clones the repository without credentials, so this works
   only while it is readable without logging in. From a private copy, install
   from a local one instead: put the contents of `addon/` into `/addons/thbr/`
   on the machine — over the *Samba share* or *Advanced SSH & Web Terminal*
   app, or any other route to that directory — and it appears under **Local
   apps** after *⋮ → Check for updates*. Everything below is the same either
   way.
2. **Install** *THBR Thread Border Router*, which now appears in the store
   under a heading of the same name.
3. **Choose the stick.** On its *Configuration* tab, pick the ESP32-C6
   under `device` — it is listed as *Espressif USB JTAG/serial debug unit*.
   Leave `flash` at `auto`. Save.

   With more than one USB device on the machine, make the stick identify
   itself: **plug it in last**, deliberately, right before this step. The
   add-on lists the ports it can see newest first and says how long ago each
   appeared, so the stick is the one at the top. After a reboot that ordering
   says nothing — every port is created within the same second at boot — so
   **unplug the stick and plug it back in** to make it the newest again.

   Picking the wrong one is not destructive: nothing is written to a port
   before it has been asked which chip it is and which application it carries,
   and an application this add-on did not build is never overwritten unasked.
   [Choosing the right port](addon/DOCS.md#choosing-the-right-port) has the
   detail.
4. **Start it** and watch the log. A stick that answers nothing is flashed
   first, which takes about a minute; lines prefixed `[stick]` come from the
   firmware itself. `br=running` means border routing is up.
5. **Give Home Assistant the interface.** *Settings → System → Network →
   Network adapter*: enable your normal adapter **and** `tap0`, then restart
   Home Assistant. It binds its discovery sockets per interface when it starts,
   so without `tap0` among them nothing that announces itself across the
   backbone reaches it — the router's own announcement, and the Matter devices
   behind it.
6. **Add the router.** *Settings → Devices & services → Add integration →
   Open Thread Border Router*, and give it `http://192.168.45.2` — the stick's
   address on the backbone, the `stick_addr` option if you changed it. Home
   Assistant then imports the Thread network from it, and Matter devices are
   commissioned as usual.

   It has to be typed in: Home Assistant discovers border routers over mDNS,
   and this router does announce itself there — but that discovery feeds the
   *Thread* integration, which lists routers. The *Open Thread Border Router*
   integration, the one that actually reads and writes the network, carries no
   mDNS discovery at all; it is set up automatically only for Home Assistant's
   own border-router add-on.

The add-on then has its own page in the sidebar, **Thread BR**: state of stick
and network, a graph of the mesh you can pull around, and the buttons for
firmware, restart and network settings. What each option does, and what to do
when a step above does not produce what it says, is in
[`addon/DOCS.md`](addon/DOCS.md).

## Install with Docker

For Home Assistant in a container of your own. The image is on Docker Hub as
[`tostmann/thbr`](https://hub.docker.com/r/tostmann/thbr), multi-arch for
`arm64` and `amd64`:

```
docker pull tostmann/thbr:2026.8.11
```

Take the `thbr` service from [`addon/compose.yaml`](addon/compose.yaml), point
`THBR_DEVICE` at your stick's `/dev/serial/by-id/…` path, and start it. Then do
step 5 and 6 above — they are the same for both ways of running it.
[`addon/README.md`](addon/README.md) has the details, including what the
container needs from the host and why.

## How it fits together

```
Home Assistant ─ Matter server ─┐
                                │  tap0   (an interface on the host)
                          add-on / container
                                │  SLIP over CDC-ACM
                            ESP32-C6      (border router, on the chip)
                                │  802.15.4
                          Thread devices
```

The backbone between host and stick is a private point-to-point link
(`192.168.45.0/24` by default) and does not touch your LAN. The stick answers
there with an ot-br-posix-compatible REST API and reports its own state on a
second port.

## Building it yourself

```
THBR_STAGE=2 scripts/build.sh      # firmware, with ESP-IDF
scripts/dist.sh                    # collect the flash images into addon/firmware/
docker build -t thbr addon/        # the container image
```

Publishing a release builds both architectures natively and pushes them under
the version in `addon/config.yaml`:

```
THBR_REMOTE_HOST=user@other-arch-host scripts/publish_images.sh
```

`scripts/idf_env.sh` sets up the toolchain and is host-specific; it is not part
of the repository.

## Licence

**PolyForm Noncommercial License 1.0.0** — free for anything that is not
commercial. See [LICENSE](LICENSE).

The components this builds on keep their own licences, which these terms do not
narrow: OpenThread under BSD-3-Clause, the Espressif components under
Apache-2.0, cJSON under MIT, and esptool — shipped unmodified and run as a
separate program — under GPL-2.0-or-later. [NOTICE](NOTICE) lists them.
