/* THBR info API — a tiny HTTP endpoint that identifies the firmware.
 *
 * The ot-br-posix-compatible REST API (esp_ot_br_server, port 80) describes
 * the Thread network but says nothing about the firmware running it.  The
 * host-side container needs exactly that to decide whether the stick carries
 * THBR at all and whether it is the bundled version — so it lives on its own
 * port, independent of the border router coming up:
 *
 *   GET /version   {"product":"THBR","fw":"0.1.28","build":"...","chip":"esp32c6",...}
 *   GET /status    {"fw":...,"uptime_s":...,"heap":...,"link":"up","br":"running","role":"router"}
 */
#pragma once

#include "esp_err.h"
#include <stdbool.h>

/* Start the server on CONFIG_THBR_INFO_PORT.  Call once the backbone netif
 * exists; the handlers answer from then on, whatever the border router does. */
esp_err_t info_server_start(void);

/*   GET /backbone  {"omr_prefix":"fd6a:…:1::/64","onlink_prefix":"…","ll":"fe80::…"}
 *
 * /backbone exists because the host cannot always learn these from the router
 * advertisement: inside a container /proc/sys is read-only, so the two per-
 * interface sysctls that make the kernel accept a route-information option may
 * not be settable.  With the prefixes reported here the host side can install
 * the route itself instead of failing silently.
 */

/* Tell /status that the OpenThread instance (and its lock) exist.  Before
 * this, /status reports br/role as "n/a" instead of touching the stack. */
void info_server_set_ot_ready(bool ready);
