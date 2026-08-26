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

An ESP32-C5 runs this firmware too. It builds for that target unchanged, and
on hardware it carries a border router in daily use: a network saved from a C6
was restored onto one, the mesh reformed with every node, and the devices on it
went on working without being commissioned again. About 130 KB of memory stays
free there against 265 KB on a C6 — half, but steady.

A C5 build ships alongside the C6 one, and the add-on writes whichever matches
the chip it finds. One thing is worth knowing before choosing a C5. On the C5s
tested, talking to the chip over its own USB port sometimes stopped working:
the port stayed listed and enumerated, but nothing answered through it, and a
USB bus reset did not help. It did not happen every time, and it has never
happened on a C6. A board with a UART bridge makes it harmless — a reset over
the bridge always revived it, and that takes no more than pulsing DTR and RTS.
The Bluetooth proxy was measured on a C6; on a C5 it has to fit in what is left
of a smaller budget, and that has not been tested here.

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
| `stick_log` | How much of the stick's own log is repeated in this add-on's log. `quiet` (default) drops the border router's routine web and diagnostics chatter, which is the bulk of it and can otherwise push this add-on's own lines out of a rotated log within minutes. `all` repeats everything, `off` nothing. Warnings and errors are kept in every mode except `off`. |

A flash costs about a minute of border routing. The Thread network itself
survives it — it lives in the stick's flash, and no device has to be
re-commissioned.

## If your network is called `OpenThread-ESP`

Firmware before 0.1.44 had no network of its own to fall back on: a stick with
empty storage came up on the one compiled into it, and so did every other
stick built from the same sources — same name, same PAN ID, and **the same
network key**, which is a published default value. If your border router's
Thread network is named `OpenThread-ESP`, it is that network, and anyone
within radio range who knows the default can join it.

Updating the firmware does not fix this, and cannot: the update deliberately
leaves an existing network alone, because changing it would drop every
commissioned device. Getting a private network means forming a new one and
commissioning the devices onto it again — there is no way to rotate the key
underneath them from here.

Networks you created yourself, or adopted from another border router, are not
affected: they carry their own key. Check on the add-on page under Network,
or with `GET /node/dataset/active`.

## After the first start

1. Watch the add-on log. Lines prefixed `[stick]` come from the firmware
   itself; `br=running role=router` means border routing is up. A stick
   starting for the first time makes its own Thread network and names it
   `THBR-` plus the last three bytes of its MAC address — that is the name to
   look for in Home Assistant's Thread panel. The network is random and
   belongs to that stick alone: its key is not shared with any other THBR, and
   it is kept across firmware updates. It is *not* kept across an
   `esptool erase-flash`, which leaves the stick to generate a fresh one and
   the devices paired on the old network behind, so save the network data
   while the stick is healthy.
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

## Commissioning over Bluetooth

A factory-new Matter device is in no network yet, so the first conversation
with it happens over Bluetooth LE. Plenty of machines running Home Assistant
have no Bluetooth: servers, most NUCs, virtual machines. The stick has, and it
offers that radio to the Matter server.

Nothing needs configuring here. Switch **`ble_proxy` on in the Matter Server
add-on's own options**; the stick dials its proxy endpoint, announces itself,
and from then on the Matter server drives scanning and connecting through the
stick's radio. Its log names what it uses: lines reading `via proxy` and
`ProxyBleChannel` mean the stick carried the exchange.

Not every Matter server can do this. `ghcr.io/matter-js/matterjs-server:stable`
accepts a proxy radio when started with `--ble-proxy`, and the Matter Server
add-on is built on it. `ghcr.io/home-assistant-libs/python-matter-server` does
not — its greeting reports no proxy support. The add-on asks whatever answers
on the address the stick dials and says which of the two it found, so this
shows up as a sentence in the log rather than as a commissioning that fails for
no visible reason.

**Range cuts both ways.** Too close is a known problem, but too far is the one
that actually happens: measured across twenty devices on one installation,
commissioning succeeded from -43 to -80 dBm and failed at -95 dBm, where it
timed out during the Bluetooth connection (`Secure Pairing Failed`) without the
device ever reaching Thread. **Below roughly -90 dBm, bring the device near the
machine, commission it there, and install it afterwards** — a device that failed
this way keeps its commissioning window, so a retry costs nothing; one that
failed *after* joining Thread needs another factory reset.

Two details that matter if something looks wrong:

- The Matter server publishes its port on the loopback interface only, and the
  stick is a hop away on the backbone, so it cannot reach that address. The
  add-on listens on the host end of the backbone and forwards. Set
  `THBR_MATTER_ADDR` if the server is somewhere else than `127.0.0.1:5580`, or
  leave it empty to switch the forwarder off entirely.
- A server run as an ordinary container usually listens on every interface,
  which already covers the address the stick dials. Then no forwarding is
  needed and the add-on says so instead of trying. If something else holds that
  port, it says that too, and names it as the problem — the stick would
  otherwise dial into the wrong service and report no Matter server at all.
- If no Matter server is reachable, the stick keeps trying and says so once a
  minute, naming the address it dials. That line is not an error in itself —
  it is what a stick on a machine without a Matter server does.

Bluetooth and Thread share one radio on these chips. During a commissioning
run the mesh can go briefly sluggish; the firmware logs `ChannelAccessFailure`
when the Thread stack had to wait for the air. It passes when the exchange is
done.

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
**This restarts the stick.** Reading NVS needs esptool, which resets the chip
to get into the bootloader and resets it again afterwards, so the border router
reboots and the mesh is without one for about a minute. Measured on a live
installation on 2026-08-25: the stick came back on its own and every device
rejoined, but this is not something to do casually in the evening while the
lights are in use.

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

## How many devices a border router can carry

Every Matter device in the mesh registers a service with the border router
through SRP, and the router republishes each one over mDNS on the backbone so
the Matter server can find it. Its own MeshCoP service sits in the same table.
That table has a fixed size (`CONFIG_MDNS_MAX_SERVICES`, **48** since firmware
0.1.41; the ESP-IDF default of 10 was a ceiling nobody had chosen on purpose).

Measured on an installation with 20 Matter devices: **37 of the 48 slots**, so
roughly 1.75 per device — call it a ceiling around 26 devices, and rather fewer
if devices open commissioning windows, which publishes a second service each
for as long as the window is open.

**When the table is full, commissioning fails in a way that points somewhere
else.** The device joins Thread perfectly well, its SRP registration is refused,
the SRP server answers SERVFAIL, and the controller then cannot find it over
DNS-SD — so the error reads `Timeout waiting for mDNS resolution` and looks
like a radio or range problem. The border router says what really happened, in
its own log: `Cannot add more services, please increase
CONFIG_MDNS_MAX_SERVICES`. With `stick_log: quiet` that line is kept; it is an
error, and only the routine chatter is dropped.

**A factory reset does not free a slot.** Resetting a device sends no SRP
deregistration: its entry lives until the lease expires, and a device that
still has power keeps renewing it. So the entries of a fabric you have
abandoned occupy slots for as long as the hardware is switched on.

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

**Under plain Docker: Home Assistant must not start before this container.**
Home Assistant enumerates network adapters once, at startup, and binds its mDNS
sockets per interface. Docker starts containers in no particular order, so
Home Assistant can come up before the tap exists — and then it has no adapter
on the backbone, Thread discovery never sees the router, and nothing anywhere
looks like an error. The compose file next to this document expresses the
order with `depends_on: thbr: condition: service_healthy`. If you start the
containers by hand instead, either start this one first, or check after a
reboot whether Home Assistant really has the interface and restart it if not:

```
docker exec homeassistant python3 -c "import ifaddr; print([a.nice_name for a in ifaddr.get_adapters()])"
```

As an add-on under the Supervisor this cannot happen: `startup: services` puts
the add-on before Home Assistant.

**A Matter server on the same machine will not start, `errno 98` on port
5580** — the stick dials a fixed address on the backbone to offer its
Bluetooth radio, and this add-on listens there to carry that through to the
Matter server. A Matter server that binds *every* interface wants the same
port, and the two cannot share it. The add-on therefore waits for the Matter
server to answer before taking the port at all, and stands aside for good once
one does — so this should not happen. If it still does, switch the forwarder
off with `THBR_MATTER_ADDR=` (empty): a host with its own Bluetooth adapter
does not need the stick's radio, and only the radio needs forwarding.

**`br=stopped` in the log for several minutes** — the firmware detects and
repairs the most common cause itself. If it persists, open an issue and
include the add-on log. From add-on 2026.8.35 it is safe to share: the add-on
blanks out the network credentials that firmware up to 0.1.42 prints at every
boot.

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
