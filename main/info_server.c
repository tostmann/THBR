#include "info_server.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "esp_http_server.h"
#include "esp_idf_version.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "sdkconfig.h"

#include "backbone.h"
#include "version.h"

#if CONFIG_THBR_ENABLE_BORDER_ROUTER
#include "esp_openthread.h"
#include "esp_openthread_lock.h"
#include "openthread/border_routing.h"
#include "openthread/ip6.h"
#include "openthread/thread.h"
#endif

#include "esp_netif.h"
#include "esp_task_wdt.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "thbr_info";

static httpd_handle_t s_server;
static volatile bool  s_ot_ready;

void info_server_set_ot_ready(bool ready)
{
    s_ot_ready = ready;
}

static esp_err_t send_json(httpd_req_t *req, const char *body)
{
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    return httpd_resp_sendstr(req, body);
}

static esp_err_t version_get(httpd_req_t *req)
{
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_BASE);

    char body[256];
    snprintf(body, sizeof(body),
             "{\"product\":\"THBR\",\"fw\":\"%s\",\"build\":\"%s\","
             "\"chip\":\"%s\",\"idf\":\"%s\","
             "\"mac\":\"%02x:%02x:%02x:%02x:%02x:%02x\"}",
             FW_VERSION_STRING, FW_BUILD_DATE, CONFIG_IDF_TARGET, IDF_VER,
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    return send_json(req, body);
}

static esp_err_t status_get(httpd_req_t *req)
{
    const char *br = "n/a", *role = "n/a";

#if CONFIG_THBR_ENABLE_BORDER_ROUTER
    static const char *const br_state[] = { "uninitialized", "disabled", "stopped", "running" };
    static const char *const roles[]    = { "disabled", "detached", "child", "router", "leader" };
    if (s_ot_ready) {
        esp_openthread_lock_acquire(portMAX_DELAY);
        otInstance *inst = esp_openthread_get_instance();
        otBorderRoutingState brs = otBorderRoutingGetState(inst);
        otDeviceRole r = otThreadGetDeviceRole(inst);
        esp_openthread_lock_release();
        br   = (brs < 4) ? br_state[brs] : "?";
        role = (r < 5) ? roles[r] : "?";
    }
#endif

    char body[256];
    snprintf(body, sizeof(body),
             "{\"fw\":\"%s\",\"uptime_s\":%" PRId64 ",\"heap\":%" PRIu32
             ",\"heap_min\":%" PRIu32 ",\"link\":\"%s\",\"br\":\"%s\",\"role\":\"%s\"}",
             FW_VERSION_STRING, esp_timer_get_time() / 1000000,
             esp_get_free_heap_size(), esp_get_minimum_free_heap_size(),
             backbone_is_up() ? "up" : "down", br, role);
    return send_json(req, body);
}

/* The prefixes the border router advertises, plus the address it advertises
 * them from — everything the host needs to route into the mesh by hand. */
static esp_err_t backbone_get(httpd_req_t *req)
{
    char omr[64] = "", onlink[64] = "", ll[48] = "";

#if CONFIG_THBR_ENABLE_BORDER_ROUTER
    if (s_ot_ready) {
        otIp6Prefix pfx;
        esp_openthread_lock_acquire(portMAX_DELAY);
        otInstance *inst = esp_openthread_get_instance();
        if (otBorderRoutingGetOmrPrefix(inst, &pfx) == OT_ERROR_NONE) {
            otIp6PrefixToString(&pfx, omr, sizeof(omr));
        }
        if (otBorderRoutingGetOnLinkPrefix(inst, &pfx) == OT_ERROR_NONE) {
            otIp6PrefixToString(&pfx, onlink, sizeof(onlink));
        }
        esp_openthread_lock_release();
    }
#endif

    esp_ip6_addr_t a;
    if (esp_netif_get_ip6_linklocal(backbone_netif(), &a) == ESP_OK) {
        const uint8_t *b = (const uint8_t *)a.addr;
        snprintf(ll, sizeof(ll),
                 "%02x%02x:%02x%02x:%02x%02x:%02x%02x:%02x%02x:%02x%02x:%02x%02x:%02x%02x",
                 b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7],
                 b[8], b[9], b[10], b[11], b[12], b[13], b[14], b[15]);
    }

    char body[224];
    snprintf(body, sizeof(body),
             "{\"omr_prefix\":\"%s\",\"onlink_prefix\":\"%s\",\"ll\":\"%s\"}",
             omr, onlink, ll);
    return send_json(req, body);
}

/* Restart the chip.  Without this the stick cannot be brought back to a known
 * state from the host: a wedged border router keeps answering /status while
 * nothing reaches the mesh, and there is no other way in — the add-on has no
 * shell, and reflashing needs the version to differ. */
static void reboot_task(void *ctx)
{
    (void)ctx;
    vTaskDelay(pdMS_TO_TICKS(500));      /* let the HTTP response leave first */
    ESP_LOGW(TAG, "reboot requested over the info API");
    esp_restart();
}

static esp_err_t reboot_post(httpd_req_t *req)
{
    esp_err_t err = send_json(req, "{\"rebooting\":true}");
    xTaskCreate(reboot_task, "thbr_reboot", 2048, NULL, 5, NULL);
    return err;
}

esp_err_t info_server_start(void)
{
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.server_port = CONFIG_THBR_INFO_PORT;
    /* esp_ot_br_server's instance owns the default control port (32768); a
     * second httpd on the same control port fails to start. */
    cfg.ctrl_port = 32769;
    cfg.max_uri_handlers = 4;
    cfg.max_open_sockets = 3;
    cfg.lru_purge_enable = true;

    esp_err_t err = httpd_start(&s_server, &cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "httpd_start failed: %s", esp_err_to_name(err));
        return err;
    }
    const httpd_uri_t version_uri = { .uri = "/version", .method = HTTP_GET, .handler = version_get };
    const httpd_uri_t status_uri  = { .uri = "/status",  .method = HTTP_GET, .handler = status_get };
    const httpd_uri_t backbone_uri = { .uri = "/backbone", .method = HTTP_GET, .handler = backbone_get };
    httpd_register_uri_handler(s_server, &version_uri);
    httpd_register_uri_handler(s_server, &status_uri);
    const httpd_uri_t reboot_uri = { .uri = "/reboot", .method = HTTP_POST, .handler = reboot_post };
    httpd_register_uri_handler(s_server, &backbone_uri);
    httpd_register_uri_handler(s_server, &reboot_uri);
    ESP_LOGI(TAG, "info API on port %d: /version /status /backbone, POST /reboot (fw %s)",
             CONFIG_THBR_INFO_PORT, FW_VERSION_STRING);
    return ESP_OK;
}
