/* RAM-buffered log sink with UDP delivery over the PPP link — see log_sink.h. */

#include "log_sink.h"
#include "backbone.h"

#include <stdio.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

#include "esp_log.h"
#include "lwip/sockets.h"

/* Big enough to survive the whole boot: OpenThread is chatty, and with 8 KB
 * the interesting early lines (border-router init, web server start) were
 * overwritten before the link came up and the sender could drain them.  Twice
 * this cost a diagnostic round. */
#define RING_SIZE      32768
/* Long enough for a full active-dataset TLV dump (2 * 254 hex + tag).  At 256
 * the dataset came out truncated to an odd number of hex digits and the
 * joining node answered "Error 7: InvalidArgs" — a silent data loss that cost
 * a debugging round. */
#define THBR_LINE_MAX   640
#define DRAIN_CHUNK     512

static char              s_ring[RING_SIZE];
static size_t            s_head;      /* write position */
static size_t            s_tail;      /* read position  */
static SemaphoreHandle_t s_lock;
static vprintf_like_t    s_console;   /* NULL once detached */
static bool              s_detached;

static void ring_put(const char *p, size_t n)
{
    /* Overwrite oldest on overflow: recent context beats ancient context when
     * something goes wrong late in the boot. */
    for (size_t i = 0; i < n; i++) {
        s_ring[s_head] = p[i];
        s_head = (s_head + 1) % RING_SIZE;
        if (s_head == s_tail) {
            s_tail = (s_tail + 1) % RING_SIZE;
        }
    }
}

static size_t ring_get(char *out, size_t max)
{
    size_t n = 0;
    while (n < max && s_tail != s_head) {
        out[n++] = s_ring[s_tail];
        s_tail = (s_tail + 1) % RING_SIZE;
    }
    return n;
}

/* The vprintf hook.  Must not block and must not log recursively. */
static int sink_vprintf(const char *fmt, va_list ap)
{
    char line[THBR_LINE_MAX];
    va_list ap2;
    va_copy(ap2, ap);
    int len = vsnprintf(line, sizeof(line), fmt, ap2);
    va_end(ap2);

    if (len > 0) {
        size_t n = (len < (int)sizeof(line)) ? (size_t)len : sizeof(line) - 1;
        if (xSemaphoreTake(s_lock, 0) == pdTRUE) {
            ring_put(line, n);
            xSemaphoreGive(s_lock);
        }
    }

    /* Mirror to the console until PPP claims the port. */
    if (!s_detached && s_console) {
        return s_console(fmt, ap);
    }
    return len;
}

esp_err_t log_sink_init(void)
{
    s_lock = xSemaphoreCreateMutex();
    if (!s_lock) {
        return ESP_ERR_NO_MEM;
    }
    s_console = esp_log_set_vprintf(sink_vprintf);
    return ESP_OK;
}

void log_sink_detach_console(void)
{
    s_detached = true;
}

static void udp_task(void *arg)
{
    uint32_t peer = (uint32_t)(uintptr_t)arg;
    uint16_t port = (uint16_t)((peer >> 16) & 0xFFFF);   /* packed by caller */
    peer = 0;                                            /* resolved below */

    /* Without a link there is nothing to send to; wait indefinitely rather
     * than giving up, because the backlog is the interesting part. */
    while (!backbone_is_up()) {
        vTaskDelay(pdMS_TO_TICKS(200));
    }
    peer = backbone_peer_ipv4();

    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) {
        vTaskDelete(NULL);
        return;
    }
    struct sockaddr_in dst = {
        .sin_family = AF_INET,
        .sin_port   = htons(port),
        .sin_addr   = { .s_addr = peer },
    };

    char chunk[DRAIN_CHUNK];
    for (;;) {
        size_t n = 0;
        if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(50)) == pdTRUE) {
            n = ring_get(chunk, sizeof(chunk));
            xSemaphoreGive(s_lock);
        }
        if (n > 0) {
            sendto(sock, chunk, n, 0, (struct sockaddr *)&dst, sizeof(dst));
        } else {
            vTaskDelay(pdMS_TO_TICKS(100));
        }
    }
}

esp_err_t log_sink_start_udp(uint32_t peer_ipv4, uint16_t port)
{
    /* peer is resolved in the task (the link may not be up yet); the port
     * rides along in the upper half of the argument word. */
    (void)peer_ipv4;
    uintptr_t arg = ((uintptr_t)port << 16);
    if (xTaskCreate(udp_task, "log_udp", 4096, (void *)arg, 3, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}
