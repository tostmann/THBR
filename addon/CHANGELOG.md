# Changelog

Versions follow the Home Assistant style, `year.month.release`. The firmware
that ships with each release carries its own number, shown on the add-on's page
next to the one installed on the stick.

## 2026.8.35

**Firmware 0.1.43** — the network key stops travelling in the log.

- **The stick no longer prints its credentials.** At every boot the firmware
  dumped the active dataset as TLV hex, because without a serial CLI that hex
  is how a second node joins a network. It is also the network key, it rode the
  UDP log sink into the container's log, and both this page and the README ask
  a user with a problem to attach that log to an issue — while the web
  interface has always refused to show the key. The firmware now names the
  network (name, channel, PAN ID, extended PAN ID, all of it in every beacon
  anyway) and says where the credentials actually live: the web interface, or
  `GET /node/dataset/active`.
- **And the add-on blanks the old line out.** Sticks already in the field keep
  sending it until they are reflashed, so the add-on redacts it on the way into
  the log — including the case where the line arrives split across two
  datagrams and the tail turns up without its label. From this release on, the
  add-on log is safe to attach to an issue.
- **A release cannot ship one firmware and promise another.** `dist.sh`
  replaces only the bundle of the chip it was just run for, and nothing
  compared the results: 2026.8.34 shipped 0.1.42 for the C6 and 0.1.40 for the
  C5 while this file said "firmware 0.1.42" and the docs promised an mDNS fix
  half the users did not get. `publish_images.sh` now checks every bundle
  against the others and against the version this file claims, and stops if
  they disagree. `THBR_ALLOW_FW_DRIFT=1` publishes a deliberate exception,
  which then belongs here in writing.
- The build-time intent check grows the backbone addresses (stick, host,
  netmask): the same class as the proxy endpoint that went stale in 2026.8.33 —
  a string that points somewhere.
- **One release, one firmware number, across chips.** The build counter
  advanced on every build, so a release covering two chips produced two
  different firmware versions — which is how 0.1.42 and 0.1.40 came to sit in
  one image. `THBR_NO_BUMP=1` holds the number for the second and further
  chips of the same release; the counter stays monotonic per release instead
  of per invocation.

## 2026.8.34

**Firmware 0.1.42** — the Bluetooth proxy dials the documented address again.

- **Fixes a regression shipped in 2026.8.33.** The firmware's BLE proxy
  endpoint had reverted to a bench address (`…:5590`) while the add-on's
  forwarder offers `…:5580`, so a stick asked to lend its radio dialled a port
  nobody serves — and said so only in a warning line every sixty seconds. The
  value had been corrected in the source two days earlier; a *generated*
  sdkconfig that had gone stale reintroduced it, because ESP-IDF does not
  re-read its defaults once a generated config exists.
- **The build now notices that.** `scripts/build.sh` checked only the plain
  generated config against its defaults; it checks every variant now, so a
  stale `sdkconfig.ble` or `sdkconfig.c5` regenerates instead of being reused.
- **And a release cannot claim a fix the binary does not have.**
  `scripts/dist.sh` refuses to bundle firmware whose configuration disagrees
  with what the repository says it should be — the reference is the tree
  (overlay, else the Kconfig default), not the build's own copy of it, because
  a stale build is perfectly self-consistent and would pass any check made
  against itself.
- The forwarder now reports a Matter server that takes no proxy radio on the
  ordinary path too, not only when the port is contested. Bridging to such a
  server works at the socket level and fails at the first commissioning, which
  is a long way from the cause.
- Documentation, all of it measured on a live installation rather than
  reasoned: **saving the stick's settings restarts it** (esptool resets the
  chip; the mesh is without a border router for about a minute — the previous
  "pauses for a second or two" was wrong); **commissioning range**, which
  succeeded from -43 to -80 dBm and failed at -95 dBm; and a new section on
  **how many devices a border router can carry** — 48 mDNS slots, about 1.75
  per device, a ceiling near 26, why a full table makes commissioning fail
  with an error that points at DNS instead, and that a factory reset frees no
  slot until the SRP lease expires.

## 2026.8.33

**Firmware 0.1.41** — the border router can carry more than nine Matter devices.

- **`CONFIG_MDNS_MAX_SERVICES` raised from the IDF default of 10 to 48.** Every
  Matter device in the mesh registers a service through SRP, and the
  advertising proxy republishes each one through the firmware's mDNS
  responder; the border router's own MeshCoP service sits in the same table.
  Ten was therefore not an allowance but a ceiling on how many devices a border
  router could carry — and nothing said so until commissioning failed. Measured
  on a live installation: with the table full, a freshly reset lamp joined
  Thread, its SRP registration was refused ("Cannot add more services, please
  increase CONFIG_MDNS_MAX_SERVICES (10)"), the SRP server answered SERVFAIL,
  and commissioning died at operational discovery — a failure that reads like a
  radio or range problem and is neither.
- The limit is a comparison threshold in the responder, not a static
  allocation: the image is byte-for-byte the same size as 0.1.40, and memory is
  only spent per service that actually exists (a few hundred bytes each).
- Also worth knowing for anyone re-commissioning a fabric: a device that is
  factory reset does not deregister its SRP entry. It lingers until the lease
  runs out, and a device that still has power keeps renewing it — so stale
  registrations from a dead fabric occupy slots for as long as the hardware is
  switched on.

## 2026.8.32

Firmware unchanged at 0.1.40.

- `stick_log: quiet` now also drops the border router's `web_api` chatter — one
  "diagnostic collection complete" line every sixteen seconds, and three more
  around it. Measured after 2026.8.31 went out: that release took the stick's
  share of the log from about fifty lines a minute down to thirteen, and
  `web_api` was most of what was left. What stays are the OPENTHREAD lines,
  which say which devices registered, and anything at warning level or above.

## 2026.8.31

Firmware unchanged at 0.1.40.

- **The add-on no longer takes port 5580 away from a Matter server.** The stick
  dials a fixed address on the backbone to offer its Bluetooth radio, and the
  add-on listens there to carry that through. A Matter server run as an
  ordinary container listens on *every* interface, which is the same port, and
  two listeners cannot share it. Until now the add-on kept retrying that bind
  every 30 seconds — so it took the port the moment such a server restarted,
  and from then on that server failed to start with `errno 98`, for good. It
  now waits for the Matter server to answer before taking the port at all, and
  where the server already holds it, stands aside permanently and says so.
  Found on a live installation, where it stopped the Matter server from coming
  back up after a restart.
- A `THBR_MATTER_ADDR` pointing at the forwarder's own address is refused
  instead of forwarding to itself.
- **New option `stick_log`: `quiet` (new default), `all`, `off`.** The border
  router's own web and diagnostics logging is about fifty lines a minute,
  steady, and all of it routine — enough to push the add-on's own lines out of
  a rotated container log within minutes. `quiet` drops that running
  commentary and keeps everything else, warnings and errors included. `all` is
  the previous behaviour.
- Documented what start order means under plain Docker: Home Assistant
  enumerates its network adapters once, at startup, so it must not come up
  before the tap exists. The compose file already says so with
  `depends_on: condition: service_healthy`; started by hand, it is on you. As
  an add-on this cannot happen, `startup: services` covers it.

## 2026.8.30

Firmware unchanged at 0.1.40.

- **The two FHEM example modules are gone, and the walkthrough now ends where
  FHEM begins.** They came out of testing and were never the point: a FHEM
  module for Matter already exists and is further along —
  [fhem-matter](https://gitlab.com/zeppelin1979/fhem-matter), which talks to
  the same Matter server, is installed through FHEM's own update mechanism, and
  was tested here against this setup. Two modules in one niche help nobody, and
  a border router is what this project is for.
- What the walkthrough keeps is the part that belongs to it and holds whichever
  FHEM module someone picks: the stick, the Matter server, the Thread
  credentials it has to be given, and the pairing. Pairing has its own chapter
  now, because it is the one step that cannot happen without the stick's second
  radio: a device fresh out of its box is on no network, so the first
  conversation is over Bluetooth, and only at the end of it does it learn the
  Thread network. Handing the server the Thread credentials no longer needs a
  FHEM module either — the recipe is a dozen lines against the two APIs.

## 2026.8.29

Firmware 0.1.40.

- **The stick could stop connecting over Bluetooth until it was restarted.**
  Two attempts that ended without a link and both connection slots were gone —
  the next request was refused with "no free connection slot" while the device
  sat there being discovered. A connect event that arrived when nothing was
  waiting for it was dropped, keeping the slot and, on success, a link nobody
  would ever close; and losing the link to the Matter server left every
  connection standing although the server had forgotten them. Both are
  released now. It took a third device to hit this.
- **A restarted Matter server left the stick silent.** The client library is
  supposed to notice and dial again; measured twice, it did not — no event, no
  retry, and only a reboot of the stick brought it back. Since a Matter server
  restarts on every one of its own updates, this cannot need a human. The link
  is never quiet by itself, so a minute without a single frame is now taken as
  a dead link and dialled again from the side that owns the socket. Measured:
  noticed after 63 seconds, reconnected and shook hands on its own.

## 2026.8.28

Firmware unchanged at 0.1.38.

- The FHEM walkthrough began in the middle: it assumed a border router was
  already running and sent the reader elsewhere for it. Setting up the stick is
  now its first step, with the container that does it, the two device rules
  without which the port is visible but cannot be opened, and what a working
  one answers.

## 2026.8.27

Firmware unchanged at 0.1.38.

- `fhem/README.md` is now a walkthrough rather than a module description: from
  an IKEA lamp in its box to a switch in FHEM, including installing and running
  the Matter server, and the step that is easy to miss — handing the server the
  Thread network, without which a commissioning runs through certificates and
  fabric and then fails at the last moment. A section on what to do when
  pairing fails names the four messages that actually turned up while getting
  there. The modules are stated to be examples and a base for others to build
  on, with their gaps listed.

## 2026.8.26

Firmware unchanged at 0.1.38.

- **A paired lamp is now a lamp in FHEM.** The module used to write everything
  a node reported as readings on the one server device — complete, and useless
  as an interface. There are now two levels, the way FHEM expects: `MatterWS`
  holds the connection to the Matter server, and the new `74_MatterDevice.pm`
  turns each node into its own device with `on`/`off`/`toggle`/`pct`, its own
  readings and a `state`. Devices appear through `autocreate` on a node's first
  message, and take the product the device reports as their alias.
- **A button can drive a lamp.** Presses arrive as Matter events, not as
  attribute changes — `CurrentPosition` is back to zero before anything could
  act on it — and one press produces three of them. Only the finished gesture
  is passed on, as `ep<n>_press` with `single`, `multi2`, `long`, so a `notify`
  fires once. Measured on an IKEA button and an IKEA lamp.
- `fhem/README.md` walks through all of it with those two devices as they
  actually appear, and says what still does not work: colour is read but not
  written, and a node that stops answering looks like one that answers.

## 2026.8.25

Firmware unchanged at 0.1.38.

- **The FHEM module's pairing field now appears.** FHEMWEB asks a device which
  commands it has by calling `set` with a single `?`; the module counted its
  arguments first and answered with a usage line, so the web interface drew no
  input at all — including the field for the pairing code the module exists to
  offer. Pairing a device from FHEM has since been done end to end: the command
  goes out of FHEMWEB, the stick carries the Bluetooth, and the node turns up
  as readings.

## 2026.8.24

Firmware unchanged at 0.1.38.

- **The forwarder now says what it found.** It binds the host end of the
  backbone so the stick can reach a Matter server on loopback — but a server
  run as an ordinary container usually listens on every interface and already
  covers that address. Then binding fails, and the previous version retried
  in silence for as long as it ran: the stick reported no Matter server every
  minute and nothing on this side said why. It now asks whatever holds the
  port who it is, and says one of three things — the server is already
  reachable and no forwarding is needed, a Matter server answers but reports
  no BLE proxy so commissioning over the stick cannot work with it, or
  something else entirely is on the address the stick dials.
- Matter without Home Assistant is documented: which server accepts a proxy
  radio (`ghcr.io/matter-js/matterjs-server` with `--ble-proxy`) and which does
  not, where it has to listen, and that pairing happens in that server's own
  web page. `compose.yaml` carries it as a service.
- A FHEM module ships in `fhem/`. It turns the Matter server's devices into
  readings and `set` commands and can pair a device from FHEMWEB. Tested
  against one server and one lamp — a starting point, not a finished driver.
- Log lines are written whole. Two threads logging at once ran into each other
  now that the forwarder has one of its own.

## 2026.8.23

Firmware 0.1.38.

- **The stick lends its Bluetooth radio to the Matter server.** A Matter device
  is talked to over Bluetooth LE before it has ever seen the Thread network,
  and plenty of machines running Home Assistant have no Bluetooth: servers,
  most NUCs, virtual machines. The stick has one. Switch `ble_proxy` on in the
  Matter Server add-on's own options and the stick dials its proxy endpoint,
  announces itself, and carries the scan, the connection and the commissioning
  exchange. Nothing to configure here.
- The Matter server publishes its port on the loopback interface only, which
  the stick — a hop away on the backbone — cannot reach. The add-on listens on
  the host end of the backbone and forwards. `THBR_MATTER_ADDR` points it
  somewhere else, or switches it off when empty.
- **Firmware for the C6 and the C5 both ship**, one bundle per chip, and the
  chip found on the port decides which one is written. Refusing a chip the
  image has no firmware for now says which chips it does carry.
- The commissioning exchange asks for a bigger ATT_MTU before reporting the
  link. Reporting the 23 bytes a fresh Bluetooth link starts with made the
  transport cut every certificate into 20-byte pieces — hundreds of round trips
  on a radio that also has Thread to serve, and the exchange stalled. Measured
  on hardware: a commissioning that used to stall now completes in 22 seconds.
- A stick that finds no Matter server used to put six lines in the log every
  five seconds and push everything else out of it within a minute. It now says
  it once a minute and names the address it is dialling. A link that drops
  after being established says it was lost, rather than that no server was
  there.

## 2026.8.14

Firmware unchanged at 0.1.35.

- The page called itself after one chip. It runs border routers on more than
  one, and the chip it is talking to has always been on the page anyway, so the
  heading no longer names a model.
- What the ESP32-C5 does and does not do, from having run one: a network moved
  onto it keeps working, memory is about half, and talking to the chip over its
  own USB port sometimes stops until it is reset.

## 2026.8.13

Firmware unchanged at 0.1.35.

- Replacing the stick left the host routing into the mesh through the stick
  that had been removed. The route was only ever renewed when the network's
  prefix changed; after a replacement the prefix is deliberately the same and
  only the next hop moves, so the old route survived as a black hole that
  looked perfectly healthy. The next hop is now compared too, and a route
  pointing at a border router that is no longer there is repointed.
  Found while moving a network to replacement hardware, which is exactly the
  case the saved settings exist for.

## 2026.8.12

Documentation only. Firmware unchanged at 0.1.35.

- Saved network settings carry the router's whole identity, not only the
  network's credentials: a stick restored from another comes up under the
  original's extended address. That is what lets devices rejoin without
  noticing, and it means two sticks holding the same saved settings must never
  be on the air together. Measured, and now said.
- What is known about the ESP32-C5: the firmware builds and runs on it, but on
  the one tested, writing flash over the chip's own USB port was unreliable
  while the board's UART bridge was not.

## 2026.8.11

Firmware unchanged at 0.1.35.

- Saving and restoring the network settings named a chip, which meant a
  settings file could only be written back to a stick of the same family — and
  esptool refuses outright when they differ. Since the point of the feature is
  moving a network to replacement hardware, that restriction defeated it. The
  chip is now detected rather than asserted, as it should be for reading and
  writing a raw flash region.
- A failed restore now says why, instead of only that it failed.

## 2026.8.10

Firmware unchanged at 0.1.35.

- Example log lines and two source comments carried a hardware address from
  the machine they were written on, and three comments pointed at a file that
  is not part of this repository. Replaced with placeholders and with the
  explanation the reference stood for.
- The script that assembles the published tree is no longer part of it: it is
  maintainer tooling, and the patterns it searches for name what they exclude.

## 2026.8.9

Documentation only. Firmware unchanged at 0.1.35.

- The menu paths were out of date. Home Assistant's own strings now read
  *Apps* and *App store*, not *Add-ons* and *Add-on store*, and the local
  repository is listed as *Local apps* — checked against the frontend's
  translations rather than remembered.
- The add-on's own README still told the reader to click a border router
  offered as a discovery. That claim was corrected in the other two documents
  one release ago and missed here.

## 2026.8.8

Documentation only. Firmware unchanged at 0.1.35.

- The last installation step was wrong. It said Home Assistant offers the
  border router as a discovery; it does not. The router announces itself over
  mDNS and that is what the *Thread* integration listens for, but the *Open
  Thread Border Router* integration — the one that reads and writes the
  network — has no discovery except for Home Assistant's own add-on, so its
  address has to be typed in. Checked against a running installation.
- Installing from the repository needs the repository to be readable without
  credentials; installing from a local copy is described for when it is not.
- A picture of the add-on's page.

## 2026.8.7

Firmware unchanged at 0.1.35.

- The page now says which THBR release it is. It showed the stick's firmware
  version and nothing about itself, which left no way to tell from the page
  whether an update had actually taken effect. The release is baked into the
  image at build time, so it is also right under plain Docker.

## 2026.8.6

Firmware unchanged at 0.1.35.

- **The add-on's web interface answered anyone on the network.** It has to run
  in the host's network namespace for the tap device, which put its port on
  every interface next to Home Assistant rather than behind it — so flashing
  the stick, restarting it and downloading the Thread credentials were all
  reachable without logging in to anything. It now answers only where ingress
  reaches it from, and refuses everything else with a line in the log naming
  the source. The new `web_allow` option widens that on purpose.
- Under plain Docker there is no ingress, so the port stays open as before and
  the container now says so at startup, with the setting to narrow it.

## 2026.8.5

Documentation only. Firmware unchanged at 0.1.35.

- The installation instructions now say to plug the stick in last, on purpose,
  right before choosing it — and to unplug and replug it after a reboot, where
  every port carries the same boot timestamp and the ordering says nothing.

## 2026.8.4

Firmware unchanged at 0.1.35.

- The add-on now also reads which application is on a stick before writing, and
  refuses to overwrite one it did not build. The previous release checked the
  chip, which does not distinguish this stick from any other ESP32-C6 board on
  the same machine — they carry the same chip on the same kind of port.
  Converting such a board is still possible, but has to be asked for.
- With no device configured, the candidate ports are listed newest first with
  how long ago each was plugged in, so plugging the stick in last is enough to
  identify it.

## 2026.8.3

Firmware unchanged at 0.1.35.

- The add-on asks a port what chip is on it before writing anything, and
  refuses unless it is the chip the bundled firmware is for and the one the
  port is named after. Picking the wrong serial device out of the list was
  previously caught only by esptool, late and in words about image headers.
- A device that turns out to be something else is reset back out of the
  bootloader instead of being left in it.
- The log now names the chip the add-on adopted, and a port given as a bare
  `/dev/ttyACM3` is found again by its MAC when that number moves.

## 2026.8.2

Firmware unchanged at 0.1.35.

- Published on Docker Hub, one image per architecture plus a multi-arch tag
  `tostmann/thbr` for running it under plain Docker. Installing the add-on now
  pulls that image instead of building the directory on your machine.
- Step-by-step installation instructions for both ways of running it.

## 2026.8.1

First release. Firmware 0.1.35.

- Runs the border router on the stick and carries the backbone over its USB
  port; installs the route into the mesh itself where the host cannot learn it
  from the router's advertisements.
- Flashes the stick on first start and recovers on its own when the stick is
  unplugged, reset, or stops answering.
- A page in the sidebar: state of the stick and the network, a graph of the
  mesh that can be pulled around, and buttons to write the firmware, restart the
  stick, and save its network settings.
- Saved network settings travel in Home Assistant backups and can be written
  back onto a replacement stick, which moves the Thread network to new hardware
  without re-commissioning devices.
