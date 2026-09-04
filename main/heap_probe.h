/* Heap census for the drift investigation — see heap_probe.c.
 *
 * Compiled only when CONFIG_THBR_HEAP_PROBE is set, so a release build carries
 * neither the endpoint nor the tracing buffer.
 */
#pragma once

#include "esp_err.h"
#include "esp_http_server.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Registers GET/POST /heap on an already started server.  Returns the number
 * of URI handlers it took, so the caller can keep max_uri_handlers honest. */
int heap_probe_register(httpd_handle_t server);

#ifdef __cplusplus
}
#endif
