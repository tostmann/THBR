# Matter in FHEM

Two modules, and a Matter server they talk to. `73_MatterWS.pm` holds the
connection; `74_MatterDevice.pm` turns each node the server reports into a FHEM
device with its own readings and switches. Someone who pairs a lamp gets a
lamp, not thirty readings on a server device.

The Matter protocol itself — commissioning, certificates, PASE and CASE — runs
in the Matter server. These modules speak its WebSocket API and nothing else.

## What you need

- A **Matter server** that FHEM can reach. `ghcr.io/matter-js/matterjs-server`
  is the one this was written against. Start it with `--ble-proxy` if you want
  to commission over a THBR stick's Bluetooth radio; see the project README for
  why the other implementation cannot do that.
- A **Thread border router**, if your devices are Thread devices. THBR is one.
- **FHEM** with `autocreate` defined — that is what makes paired devices turn
  up by themselves.

## Install

Copy both modules into FHEM's module directory and tell FHEM about them:

```
cp 73_MatterWS.pm 74_MatterDevice.pm /opt/fhem/FHEM/
```
```
reload 73_MatterWS.pm
reload 74_MatterDevice.pm
```

Then define the connection to the server, host and port as yours are:

```
define MatterWS MatterWS ws:matter-host:5580/ws
attr MatterWS room Matter
```

`STATE` goes to `listening` once it is talking to the server, and the readings
`sdkVersion`, `fabricId` and `nodes` say what it found.

## Pairing

From FHEM:

```
set MatterWS pair 1234-567-8901
```

The `pairing` reading follows along: `running`, then `node 18 added`, or
`failed: …` with what the server said. The server's own web page on the same
port does the same thing if you prefer clicking.

For a factory-new Thread device the first conversation happens over Bluetooth
LE. If the machine running FHEM has no Bluetooth — servers and NUCs usually do
not — a THBR stick lends its radio to the Matter server, and the pairing goes
out over that. Nothing needs configuring in FHEM for it.

## What appears

Two IKEA devices, as they turned up here after pairing. Nothing was defined by
hand; `autocreate` made both from the first message each node sent.

```
define Matter_18 MatterDevice 18
attr   Matter_18 alias KAJPLATS GU10 CWS 470lm

   Internals:  NODE 18   IODev MatterWS   STATE on
   Readings:   state            on
               ep1_onoff        on
               ep1_brightness   254
               ep1_colorMode    2
               ep1_colorTempMireds 370
               0_40_1           IKEA of Sweden
               0_40_3           KAJPLATS GU10 CWS 470lm
```

```
define Matter_17 MatterDevice 17
attr   Matter_17 alias BILRESA dual button

   Internals:  NODE 17   IODev MatterWS   STATE present
   Readings:   0_40_1           IKEA of Sweden
               0_40_3           BILRESA dual button
               1_59_0           2        <- switch, endpoint 1, positions
               2_59_0           2        <- switch, endpoint 2
```

The alias comes from the product name the device reports, so the list reads
like the shelf it came from rather than like a node table.

Readings are named `ep<endpoint>_<what>` for the clusters the module knows
(`onoff`, `brightness`, `colorTempMireds`, `colorMode`, `localTemperature`) and
`<endpoint>_<cluster>_<attribute>` for everything else. Nothing is dropped.

## Switching

```
set Matter_18 on
set Matter_18 off
set Matter_18 toggle
set Matter_18 pct 40
```

For buttons in the room view:

```
attr Matter_18 webCmd on:off:toggle:pct
attr Matter_18 devStateIcon on:light_light_dim_100 off:light_light_dim_00
```

A device whose switch is not on endpoint 1 — none seen so far — takes
`attr <name> endpoint <n>`.

## Linking a button to a lamp

A press is a moment, not a state: `CurrentPosition` is back to zero before
anything could act on it. So presses arrive as Matter events, and the module
turns the ones that mean a finished gesture into a reading:

| reading          | when                                             |
|------------------|--------------------------------------------------|
| `ep<n>_press single` | a press, released                            |
| `ep<n>_press multi2` | pressed twice, and so on                     |
| `ep<n>_press long`   | held                                         |

A single press produces three Matter events — InitialPress, ShortRelease,
MultiPressComplete — so acting on all of them would fire three times. Only the
completed gesture is passed on, once.

The two buttons of a BILRESA are endpoints 1 and 2. Which one is up is a matter
of how it hangs on the wall; press one and see which reading moves.

```
define bilresa_on  notify Matter_17:ep1_press:.single set Matter_18 on
define bilresa_off notify Matter_17:ep2_press:.single set Matter_18 off
```

Measured here, from two separate FHEM logs:

```
20:25:33  Matter_17  ep1_press: single   ->   20:25:33  Matter_18  on
20:25:35  Matter_17  ep2_press: single   ->   20:25:35  Matter_18  off
```

Dimming on a long press is the same shape, with `pct` instead of `on`.

## What does not work yet

- **Colour is read, not written.** `ep1_colorTempMireds` and `ep1_colorMode`
  come in; there is no `set` for them.
- **A node that stops answering looks the same as one that answers.** The
  server knows (`available`), the device does not show it. A device removed at
  the far end also stays until you `delete` it.

## Removing a device

Unpair at the server and drop the FHEM device:

```
{ MatterWS_Send($defs{MatterWS}, { command => "remove_node", args => { node_id => 18 } }) }
delete Matter_18
```

## Tested against

One Matter server (matter.js), one lamp and one button, on one evening —
pairing, autocreate, switching, and the button driving the lamp were all done
on that hardware. Treat everything not written above as untried.
