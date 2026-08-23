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

- A **Thread border router** — an ESP32-C6 stick running THBR, reached over the
  chip's own USB port. Step 1 sets it up and says what else works.
- A **Matter server**. Matter itself — commissioning, certificates, PASE and
  CASE — happens there, not in FHEM.
- **FHEM**, with `autocreate` defined.

## 1. The border router

**The hardware:** an **ESP32-C6** with 4 MB flash, connected by its **native
USB port** — the chip's own, not a UART bridge on the same board. It shows up
as an *Espressif USB JTAG/serial debug unit*. That is the combination this was
built and measured on.

An **ESP32-C5** runs the same firmware and the container carries a build for
both, picking the one that matches the chip it finds. It has about half the
memory to spare, and on the C5s tested here the native USB port occasionally
stopped answering until the chip was reset — see `addon/DOCS.md` before
choosing one. The **ESP32-H2** cannot run this at all: Espressif ships no
border-router library for it.

The stick carries the whole border router; the host only needs the container
that turns its USB port into a network interface.

Plug the stick in and find its port. The name carries the chip's MAC, which is
how you tell it from every other serial device on the machine:

```
ls -l /dev/serial/by-id/
```

Plugging the stick in **last** makes this unambiguous — it is the newest entry.
Then:

```
docker run -d --name thbr --network host --restart unless-stopped \
  --cap-add NET_ADMIN --cap-add NET_RAW \
  --device-cgroup-rule "c 166:* rmw" --device-cgroup-rule "c 10:200 rmw" \
  -v /dev:/dev -v /proc/sys/net/ipv6/conf:/hostsys -v thbr-data:/data \
  -e THBR_DEVICE=/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_XX:XX:XX:XX:XX:XX-if00 \
  tostmann/thbr:latest
```

- `--device-cgroup-rule` twice: without them the container sees the port and is
  not allowed to open it, which looks exactly like a broken stick.
- `-v /proc/sys/net/ipv6/conf:/hostsys` lets it set the two sysctls the host
  needs to learn the route into the mesh. Without it the container says so and
  installs the route by hand instead.
- `-v thbr-data:/data` holds the backups of the stick's network settings.
- `--network host` because the tap has to live in the host's namespace.

A stick with no THBR firmware on it gets flashed on first start; one that
already runs it is left alone. `docker logs thbr` says which, and the whole
thing is also a Home Assistant add-on — see the project README.

When it is up:

```
$ curl -s http://192.168.45.2:8082/status
{"fw":"0.1.38","uptime_s":1930,"heap":196348,"link":"up","br":"running","role":"leader"}
$ ip -6 route | grep tap0
fd8b:f497:3b84:1::/64 via fe80::… dev tap0
```

`br: running` and a route through `tap0` are what the rest of this depends on.
The stick also serves a page on port 8099 with the same information, the mesh
topology, and buttons to update its firmware or save its network settings — it
is open to the whole host by default, so put it behind something or set
`THBR_WEB_ALLOW` to a network you trust.

## 2. The Matter server

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

## 3. Give the server the Thread network

**The step that is easy to miss.** A commissioning runs through certificates
and fabric, and then fails at the last moment with

```
No Wi-Fi/Thread network credentials are configured for commissioning
```

because the server has no idea which Thread network to send the device to. The
border router knows; hand it over once.

The border router knows the network; the server has to be told once. Both ends
speak for themselves — the stick over its REST API, the server over the
websocket the FHEM module will later use — so a dozen lines are enough, and
this needs no FHEM module at all:

```python
import base64, json, os, socket, struct, urllib.request
HOST, PORT = "matter-host", 5580        # the Matter server
STICK      = "192.168.45.2"             # the border router

ds = urllib.request.urlopen(urllib.request.Request(
        f"http://{STICK}/node/dataset/active",
        headers={"Accept": "text/plain"})).read().decode().strip()

s = socket.create_connection((HOST, PORT), timeout=10)
s.sendall((f"GET /ws HTTP/1.1\r\nHost: {HOST}:{PORT}\r\nUpgrade: websocket\r\n"
           "Connection: Upgrade\r\n"
           f"Sec-WebSocket-Key: {base64.b64encode(os.urandom(16)).decode()}\r\n"
           "Sec-WebSocket-Version: 13\r\n\r\n").encode())
buf = b""
while b"\r\n\r\n" not in buf:
    buf += s.recv(4096)

p = json.dumps({"message_id": "1", "command": "set_thread_dataset",
                "args": {"dataset": ds}}).encode()
mask = os.urandom(4)
s.sendall(struct.pack("!BBH", 0x81, 0x80 | 126, len(p)) + mask +
          bytes(b ^ mask[i % 4] for i, b in enumerate(p)))
print(f"dataset of {len(ds)} characters handed over")
```

The frame header above assumes the message is longer than 125 bytes, which it
always is: the dataset alone is around 220 characters.

`thread_credentials_set` in the server's greeting turns `true` and stays that
way across restarts. Under Home Assistant this happens by itself; on a plain
host it does not.

## 4. Pairing

This is where the stick earns its second job, so it is worth understanding
rather than following.

A Matter device that has just come out of its box is on no network at all. It
cannot be talked to over Thread, because it has not been told which Thread
network to join — that is what pairing is for. So the first conversation
happens over **Bluetooth LE**, and only at the end of it does the device
receive the Thread credentials, join the mesh, and become reachable over IP.

Which means a machine without Bluetooth cannot pair a Thread device, however
good its Matter server is. Servers, most NUCs and virtual machines have none.
The stick does, and it lends it: the Matter server drives the scan, the
connection and the whole exchange, and the stick carries them over the air.
Nothing about this is configured in FHEM — it happens a layer below.

**Where the code comes from.** A factory-new device carries an 11-digit code on
the box or the device. A device that is already paired somewhere else does not
use that code any more: its owner has to open a commissioning window, which
produces a fresh one.

**Doing it.** The commissioning is the Matter server's job. Its own page on the
port from step 2 has the field for the code, and it is also what a FHEM module
asks for you.

What then happens, and what the logs show while it does:

```
Discovered commissionable device … via proxy      the stick's radio found it
Connected to …, BTP segment size=244 bytes        a link, and a useful one
  (peripheral ATT_MTU up to 247)
operationalCredentials.addNoc  statusCode: 0      the device accepts the fabric
NetworkCommissioning.Thread                       here the Thread credentials go over
connectNetwork  networkingStatus: 0               the device joins the mesh
commissioningComplete  errorCode: 0
```

The session identifier in the server's log loses its `(ble)` mark around the
`Reconnect` step: from there the device is reached over Thread, and Bluetooth
is done with. Measured here, a run takes about twenty seconds.

The larger ATT_MTU in the second line matters more than it looks. A fresh
Bluetooth link starts at 23 bytes, which cuts every certificate into 20-byte
pieces — hundreds of round trips on a radio that also has Thread to serve, and
the exchange stalls. The stick asks for a bigger one before reporting the link,
and the transport then uses 244.

## 5. Handing over to FHEM

At this point the device is on the Thread network and the Matter server knows
it. Everything after — a FHEM device per node, readings, switches — is the job
of a FHEM module talking to that same server.

[**fhem-matter**](https://gitlab.com/zeppelin1979/fhem-matter) does that. It
was tested here against the setup above: it connects to the server, creates a
device per node and endpoint, and switches a lamp. It is installed through
FHEM's own update mechanism and is developed in the
[FHEM forum thread on Matter](https://forum.fhem.de/index.php?topic=127702).

It commissions over the network only, so pairing a factory-new Thread device
stays step 4 above — the two fit together rather than overlap.

## When pairing fails

- **`discovery of node with discriminator N failed`** — the Bluetooth side
  found nothing usable. Either the device is no longer in pairing mode (the
  window closes after a few minutes), or the code does not belong to the device
  that is advertising. The server's log names the discriminator it derived from
  the code; a scan shows what the device actually advertises, and the two have
  to match.
- **`BLE proxy not connected, waiting up to 30000ms for client`** in the Matter
  server's log — no stick offered its radio. It dials the host end of its own
  backbone, and something has to answer there: the Matter server itself,
  listening on every interface, or the add-on forwarding to it. If another
  service holds that port, the add-on says so by name.
- **`No Wi-Fi/Thread network credentials are configured`** — step 3 was
  skipped. The exchange runs all the way through certificates and fabric before
  this shows up, which makes it look like a late failure rather than a missing
  setting.
- **`Commission failed: Invalid checksum`** — the code was mistyped.

## Scope

One Matter server, a handful of IKEA devices, one evening. The steps above were
walked on that hardware and the output is what it produced. This document ends
where FHEM begins, on purpose: the border router and the radio are what this
project is for.
