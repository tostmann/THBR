# Matter devices in FHEM — a worked example

From an IKEA lamp in its box to a switch in FHEM, with the steps that were
actually walked. Two modules are part of it, and they are **examples**: enough
to pair a device, read it and switch it, and a starting point for anyone who
wants more. They are not a finished Matter integration and do not try to be.

What is written here was done on real hardware. What is not written here was
not tried.

## The parts

```
IKEA device  ──802.15.4──  THBR stick  ──USB──  host  ──  Matter server  ──WS──  FHEM
                              │
                              └── Bluetooth LE, for the first conversation
```

- A **Thread border router**. THBR runs one on an ESP32-C6 or C5 and reaches
  the host over the stick's own USB port. See the project README for setting it
  up; everything below assumes it is running and the host has a route into the
  mesh.
- A **Matter server**. Matter itself — commissioning, certificates, PASE and
  CASE — happens there, not in FHEM.
- **FHEM**, with `autocreate` defined.

## 1. The Matter server

Take `ghcr.io/matter-js/matterjs-server`. It accepts a proxy radio, which is
what lets the stick do the Bluetooth part of pairing:

```
docker run -d --name matter-server --network host --restart unless-stopped \
  -v matter-data:/data \
  ghcr.io/matter-js/matterjs-server:stable \
  --storage-path /data --port 5580 --primary-interface tap0 --ble-proxy
```

- `--ble-proxy` is what makes the stick's radio usable. Without it, pairing
  needs Bluetooth on the machine itself.
- `--network host` because Matter needs mDNS and IPv6 to the mesh.
- `--primary-interface tap0` is the interface THBR creates.
- `-v matter-data:/data` — **this holds the fabric**. Lose it and every paired
  device has to be factory-reset and paired again. Do not run this container
  without it.

`ghcr.io/home-assistant-libs/python-matter-server` will not do for the
Bluetooth part: its greeting reports no proxy support, and the stick's offer is
turned away. It works for everything else.

If FHEM runs in a container of its own, the server has to listen on an address
that container can reach; `--listen-address` takes one and can be repeated.

## 2. Give the server the Thread network

**The step that is easy to miss.** A commissioning runs through certificates
and fabric, and then fails at the last moment with

```
No Wi-Fi/Thread network credentials are configured for commissioning
```

because the server has no idea which Thread network to send the device to. The
border router knows; hand it over once.

Read the active dataset from the stick:

```
curl -H "Accept: text/plain" http://192.168.45.2/node/dataset/active
```

and give it to the server — from FHEM, once the device below is defined:

```
{ MatterWS_Send($defs{MatterWS}, { command => "set_thread_dataset",
                                   args => { dataset => "0e08...18" } }) }
```

`thread_credentials_set` in the server's greeting turns `true` and stays that
way across restarts. Under Home Assistant this happens by itself; on a plain
host it does not.

## 3. FHEM

Copy both modules in and tell FHEM about them:

```
cp 73_MatterWS.pm 74_MatterDevice.pm /opt/fhem/FHEM/
```
```
reload 73_MatterWS.pm
reload 74_MatterDevice.pm
```

Define the connection, host and port as yours are:

```
define MatterWS MatterWS ws:matter-host:5580/ws
attr MatterWS room Matter
attr MatterWS webCmd pair
attr MatterWS webCmdLabel Pairing code
```

`STATE` goes to `listening`, and the readings `sdkVersion`, `fabricId` and
`nodes` say what it found. If it stays `disconnected`, nothing is listening at
that address.

## 4. Pair a device

Put the device into pairing mode the way its manual says — factory-new IKEA
devices are already in it — and give FHEM the code from the box:

```
set MatterWS pair 1234-567-8901
```

The `pairing` reading follows: `running`, then `node 18 added`, or `failed: …`
with the server's own words. It takes a minute or two: Bluetooth first, then
the device joins the Thread mesh and the rest happens over that.

The Matter server's own web page on the same port does the same thing if you
prefer clicking, and shows the same fabric.

## 5. What appears

Nothing below was defined by hand. `autocreate` made both devices from the
first message each node sent.

```
define Matter_18 MatterDevice 18
attr   Matter_18 alias KAJPLATS GU10 CWS 470lm

   Internals:  NODE 18   IODev MatterWS   STATE on
   Readings:   state                on
               ep1_onoff            on
               ep1_brightness       254
               ep1_colorMode        2
               ep1_colorTempMireds  370
               0_40_1               IKEA of Sweden
               0_40_3               KAJPLATS GU10 CWS 470lm
```

```
define Matter_17 MatterDevice 17
attr   Matter_17 alias BILRESA dual button

   Internals:  NODE 17   IODev MatterWS   STATE present
   Readings:   0_40_3               BILRESA dual button
               1_59_0               2      <- switch, endpoint 1
               2_59_0               2      <- switch, endpoint 2
```

The alias comes from the product name the device reports, so the list reads
like the shelf the things came from.

Readings are `ep<endpoint>_<what>` for the clusters the module knows — `onoff`,
`brightness`, `colorTempMireds`, `colorMode`, `localTemperature` — and
`<endpoint>_<cluster>_<attribute>` for everything else. Nothing is dropped;
extending the first list is a two-line change in `73_MatterWS.pm`.

## 6. Switching the lamp

```
set Matter_18 on
set Matter_18 off
set Matter_18 toggle
set Matter_18 pct 40
```

Buttons in the room view:

```
attr Matter_18 webCmd on:off:toggle:pct
attr Matter_18 devStateIcon on:light_light_dim_100 off:light_light_dim_00
attr Matter_18 room Matter
```

A device whose switch is not on endpoint 1 — none seen so far — takes
`attr <name> endpoint <n>`.

## 7. The button switching the lamp

A press is a moment, not a state: `CurrentPosition` is back to zero before
anything could act on it. So presses arrive as Matter events, and the module
turns the ones that mean a finished gesture into a reading:

| reading                | when                          |
|------------------------|-------------------------------|
| `ep<n>_press single`   | a press, released             |
| `ep<n>_press multi2`   | pressed twice, and so on      |
| `ep<n>_press long`     | held                          |

One press produces three Matter events — InitialPress, ShortRelease,
MultiPressComplete — so acting on all of them would fire three times. Only the
completed gesture is passed on, once.

The two buttons of a BILRESA are endpoints 1 and 2. Which one is up depends on
how it hangs; press one and watch which reading moves.

```
define bilresa_on  notify Matter_17:ep1_press:.single set Matter_18 on
define bilresa_off notify Matter_17:ep2_press:.single set Matter_18 off
```

Measured here, from two separate FHEM logs:

```
20:25:33  Matter_17  ep1_press: single   ->   20:25:33  Matter_18  on
20:25:35  Matter_17  ep2_press: single   ->   20:25:35  Matter_18  off
```

Dimming on a long press is the same shape with `pct` instead of `on`.

Remember `save` — everything defined above lives only until FHEM restarts.

## When pairing fails

- **`failed: … discovery of node with discriminator N failed`** — the Bluetooth
  side found nothing. Either the device is not in pairing mode any more (the
  window closes after a few minutes), or the Matter server has no proxy radio.
  Its log says `BLE proxy not connected, waiting up to 30000ms for client` when
  the stick never dialled in.
- **The stick logs `no Matter server at ws://192.168.45.1:5580/ble`** — it
  dials the host end of its own backbone. Something has to answer there: either
  the Matter server itself, listening on every interface, or the THBR add-on
  forwarding to it. If another service holds that port, the add-on says so by
  name.
- **`No Wi-Fi/Thread network credentials are configured`** — step 2 was
  skipped.
- **`failed: Commission failed: Invalid checksum`** — the code was mistyped.

## What these modules do not do

Named plainly, because they are examples and the gaps are where the work is:

- **Colour is read, not written.** `ep1_colorTempMireds` and `ep1_colorMode`
  arrive; there is no `set` for them.
- **A node that stops answering looks like one that answers.** The server knows
  (`available`); the device does not show it. A node unpaired at the server
  stays in FHEM until you `delete` it.
- **Only OnOff and LevelControl are driven.** Thermostats, covers, sensors and
  everything else arrive as readings and can be read, but have no `set`.
- **No `get`.** Values arrive when the device sends them.

Unpairing, when you want it:

```
{ MatterWS_Send($defs{MatterWS}, { command => "remove_node",
                                   args => { node_id => 18 } }) }
delete Matter_18
```

## Scope

One Matter server, one lamp, one button, one evening. Pairing, autocreate,
switching and the button driving the lamp were all done on that hardware, and
the output above is what it produced. Everything else is untried, and these
modules are meant as a base for the community to build on rather than as a
finished thing.
