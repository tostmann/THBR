/*
 * Log transport for a stick whose only serial port belongs to PPP.
 *
 * The C6 has exactly one USB-Serial/JTAG port and PPP takes it over (see
 * ppp_link.h), so from that moment on ESP_LOG output has nowhere to go.  This
 * sink buffers log lines in RAM and, once the PPP link is up, streams them to
 * the host as UDP syslog-ish datagrams — the backlog first, so the messages
 * produced *during* link negotiation are not lost.
 *
 * Bench usage on the host:   nc -u -l -p 5514
 *
 * Boot messages before the handover still appear on the USJ port in plain
 * text; that grace window is what makes "did the firmware even boot?"
 * answerable without a UART adapter.
 */

#pragma once

#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Install the buffering vprintf hook.  Output still goes to the console as
 * well until log_sink_detach_console() is called. */
esp_err_t log_sink_init(void);

/* Stop writing to the console (the USJ port is about to become PPP's).
 * Lines keep accumulating in the RAM buffer. */
void log_sink_detach_console(void);

/* Start the UDP sender task.  Waits for the PPP link internally, then drains
 * the backlog and keeps streaming.  peer_ipv4 == 0 means "use the PPP peer". */
esp_err_t log_sink_start_udp(uint32_t peer_ipv4, uint16_t port);

#ifdef __cplusplus
}
#endif
