# THBR — Thread Border Router on a USB stick

This add-on turns an ESP32-C6 stick into a Thread border router for Home
Assistant. The stick runs the whole border router itself and reaches Home
Assistant through its USB port, which the add-on turns into a network
interface (`tap0`). There is no radio co-processor on this machine, no
Ethernet chip on the stick, and the Thread network keeps running while Home
Assistant restarts or this add-on is updated.

The add-on also carries the stick's firmware and can flash it over the same
USB port, so a new stick needs nothing but being plugged in.

## Before you start

- An ESP32-C6 board with 4 MB flash, connected by its **native USB port**
  (USB-Serial/JTAG). It appears in the add-on's device list as an
  *Espressif USB JTAG/serial debug unit*.

An ESP32-C5 runs this firmware too — it builds for that target unchanged, and
on hardware it came up as a border router and joined a mesh alongside a C6. No
C5 build ships here, and one caveat is worth knowing before anyone tries: on
the C5 tested, writing flash over the chip's own USB port was unreliable and
the chip did not always restart afterwards, while the same operations over the
board's UART bridge worked every time. The C6 shows none of that. Roughly half
the free memory remains on the C5 (about 130 KB against 265 KB), which was
enough in the test but has not been watched over days.

Installing and updating are ordinary add-on operations: the image is published
per architecture and the Supervisor pulls it, so an update is a download rather
than a build. A new add-on version usually also carries new stick firmware —
the add-on's page shows both numbers side by side and writes it when you ask
it to, never on its own.

## Choosing the right port

The `device` list offers every serial port on the machine, and on a system with
a few USB devices the wrong one is one click away. Three things keep that from
mattering.

**The port names the chip.** THBR runs on the C6's own USB port, which
enumerates as `usb-Espressif_USB_JTAG_serial_debug_unit_<MAC>-if00`. That MAC
is the chip's, so the name identifies one specific device and keeps doing so
when `ttyACM` numbers move around after a replug. The add-on says at startup
which chip it adopted, and says it plainly when the chosen port is not one of
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

## Configuration

```yaml
device: /dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_XX:XX:XX:XX:XX:XX-if00
flash: auto
```

| Option | Meaning |
|---|---|
| `device` | The stick's serial port, picked from the list of serial devices the host offers. |
| `flash` | `auto` (default) flashes only a stick that answers nothing at all — a new stick, or one carrying a different firmware that does not respond. `upgrade` additionally replaces an older THBR when the add-on brings a newer one. `never` never writes the flash. |
| `tap` | Name of the network interface the add-on creates. Default `tap0`. |
| `web_allow` | Who may reach the add-on's page. Unset means Home Assistant only, which is what you want. `any` opens it to everything this machine is reachable on; a comma-separated list of addresses or networks opens it to those. |
| `host_addr` / `stick_addr` | The private point-to-point addresses of the backbone, `192.168.45.1/24` and `192.168.45.2`. Change them only if that subnet collides with something on your network. |

A flash costs about a minute of border routing. The Thread network itself
survives it — it lives in the stick's flash, and no device has to be
re-commissioned.

## After the first start

1. Watch the add-on log. Lines prefixed `[stick]` come from the firmware
   itself; `br=running role=router` means border routing is up.
2. **Settings → System → Network → Network adapter**: enable your normal
   network adapter **and** `tap0`, then restart Home Assistant. Home Assistant
   binds its mDNS sockets per interface when it starts, so without this it
   never sees the router.
3. **Settings → Devices & services → Add integration → Open Thread Border
   Router**, and give it `http://192.168.45.2` (the `stick_addr` option, if you
   changed it). Home Assistant imports the Thread network from the router.
   This one is typed in rather than offered: the router does announce itself
   over mDNS, but that feeds the *Thread* integration; the *Open Thread Border
   Router* integration has no discovery except for Home Assistant's own
   border-router add-on.
4. Matter devices are commissioned as usual. If a brand-new device is not
   found, make sure the commissioning dialog is not restricted to network-only
   discovery — a factory-new Thread device is in no network yet and has to be
   found over Bluetooth.

## The add-on's own page

The add-on appears in the sidebar as **Thread BR**. It shows what the log
otherwise buries — the firmware on the stick against the one this add-on
carries, whether border routing is up, the role, free memory, uptime, whether
the route into the mesh exists and whether anything actually answers through
it — and it carries the two actions that used to need a shell:

- **Update firmware** writes the bundled images to the stick over its USB port.
  The mesh pauses for about a minute; the Thread network itself survives and no
  device has to be re-commissioned. If the stick already runs the bundled
  version, the page says so and writes it again only if you confirm.
- **Restart stick** reboots it without touching the flash.

**Border router GUI** opens the router's own web interface — it lives on the
private backbone where no browser can reach it, so the add-on passes it through.

If the page does not appear in the sidebar after installing from the command
line rather than the store, the panel has to be switched on once:
`POST /addons/local_thbr/options` with `{"ingress_panel": true}`.

## Who can reach the page

Only Home Assistant, unless you say otherwise.

The add-on has to run in the host's network namespace — that is the only place
the `tap` device is of any use — and a consequence is that its web port sits on
every interface this machine has, next to Home Assistant rather than behind it.
That page can flash the stick, restart it, and hand out a copy of the Thread
credentials, so leaving it open would put all three on the LAN for anyone who
guesses the port.

So the add-on answers only where Home Assistant's ingress reaches it from: the
Supervisor's own network, `172.30.32.0/23` and `fd0c:ac1e:2100::/48`, plus
loopback. Anything else is refused with a 403 and one line in the log naming
the source. Ingress itself is unaffected — it dials the add-on from inside that
network, which is why the port cannot simply be bound to loopback instead.

The check is on the source address, not on a header. Home Assistant marks
proxied requests with `X-Ingress-Path`, but anything on the LAN can set that
header itself, which makes it a label rather than a lock.

`web_allow` widens this deliberately — `any`, or a list like
`192.168.1.0/24, 192.168.1.7`. Running under plain Docker there is no ingress and
nothing to authenticate against, so the port stays open there and the add-on
says so at startup.

## Reading the topology

The graph shows every relationship the network reports: a line between two
routers carries the link quality in both directions and the cost of the route,
a dashed line marks a child, and a ring marks the leader. Line thickness and
colour follow the link quality, so a weak neighbour is visible at a glance.

Drag any node with the mouse and the rest of the network follows — useful for
untangling a dense mesh. Positions stay put while the page refreshes; the graph
is only rebuilt when the network itself changes.

## Saving the stick's network settings

**Save network settings** reads the stick's NVS partition — the Thread network
it rejoins with and the prefix it advertises. That is the only part of the
flash worth keeping: everything else is firmware this add-on already carries.
The backbone pauses for a second or two while it is read.

The file lands in the add-on's own data directory, which means **Home Assistant
backups contain it** — verified: a partial backup of this add-on holds
`data/backups/nvs-….bin`. The page also offers it for download.

Each saved file offers **restore** next to it, which writes it back onto the
stick. That is what makes a replacement stick take over the old one's network:
same key, same PAN ID, same channel, so the devices in the mesh rejoin on their
own and none of them has to be commissioned again — verified on real hardware
by swapping the stick and bringing a Matter lamp back without touching it.
Keep the file safe: it carries the credentials of your Thread network.

**One saved file, one running stick.** What is saved is not only the network's
credentials but the router's whole identity, down to the address the mesh knows
it by — which is exactly why the devices rejoin without noticing. Measured: a
stick restored from another one comes up under the original's extended address,
not its own. So a restored stick replaces the old one; it does not join it. Two
sticks carrying the same saved settings must never be on the air at once.

A whole-flash backup is not offered, and would not work: reading 4 MB over the
stick's USB-Serial/JTAG port aborts, while this 24 KB partition reads in a
fraction of a second (both measured).

## What the add-on does to your host network

- It creates the `tap` interface and gives it the backbone address.
- It makes sure the host can actually reach the Thread mesh. A border router
  announces the route to its network in its router advertisements, and not
  every system picks such a route up — the kernel needs a per-interface option
  that an add-on cannot set, and Home Assistant OS handles advertisements in
  NetworkManager, which does not manage a tap interface. So the add-on checks
  whether the route appeared on its own and installs it otherwise, saying so in
  the log. It keeps out of the way where the system does the job: a
  hand-installed route is withdrawn again as soon as an advertised one shows
  up, and it is replaced when the border router announces a different network.

## Troubleshooting

**`waiting for /dev/serial/by-id/...`** — wrong path, or the stick is not
plugged in.

**The add-on is running but no border router is discovered** — step 2 above:
`tap0` is not among Home Assistant's network adapters, or Home Assistant
started before the interface existed.

**`br=stopped` in the log for several minutes** — the firmware detects and
repairs the most common cause itself. If it persists, open an issue and
include the add-on log.

**After flashing, the stick does not come back** — flashing resets the stick,
which makes it disconnect and re-appear on the USB bus, possibly under a
different name. The add-on waits for it and restarts the link by itself;
restart the add-on if it does not recover within a minute.

**A device commissioned right next to the stick keeps dropping out** — move it
away. Bluetooth commissioning wants the device close, 802.15.4 does not: at a
few centimetres the receiver is overloaded, and the link fails while Thread
still reports the best possible link quality. Measured on this hardware: 100 %
packet loss at 500 bytes when adjacent, 0 % at two metres.

**No Thread devices reachable, although the border router is running** — the
add-on reports this itself: every ten minutes it pings an address inside the
Thread network and logs either `mesh reachable: …` with the round-trip time, or
`MESH NOT REACHABLE`. If the route is in place and nothing answers, something
between the host and the interface is filtering.

## Its own network

The backbone is private and does not touch your LAN: the host side is
`192.168.45.1`, the stick `192.168.45.2`. The stick answers there with an
ot-br-posix-compatible REST API on port 80 (`/node`, `/node/dataset/active`,
`/diagnostics`) and reports its firmware on port 8082 (`/version`, `/status`,
`/backbone`).
