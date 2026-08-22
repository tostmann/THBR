/* PPP netif on the USB-Serial/JTAG port — see ppp_link.h. */

#include "backbone.h"

#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"

#include "driver/usb_serial_jtag.h"
#include "esp_check.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_netif_net_stack.h"   /* esp_netif_get_netif_impl */
#include "esp_netif_ppp.h"
#include "lwip/netif.h"

static const char *TAG = "ppp";

#define LINK_UP_BIT BIT0

static esp_netif_t     *s_netif;
static EventGroupHandle_t s_events;
static volatile uint32_t s_peer_ipv4;

/* netif -> serial.  The netif hands us an already HDLC-framed packet.
 *
 * Bounded write, deliberately: on USB-Serial/JTAG the TX ring only drains
 * while a host actually reads the port.  An unbounded write would wedge the
 * lwIP thread whenever pppd is stopped; with a timeout the frame is dropped
 * and PPP's own retransmission recovers it. */
static esp_err_t ppp_transmit(void *h, void *buffer, size_t len)
{
    (void)h;
    usb_serial_jtag_write_bytes(buffer, len, pdMS_TO_TICKS(500));
    return ESP_OK;
}

static const esp_netif_driver_ifconfig_t s_driver_cfg = {
    .handle   = (void *)1,   /* singleton driver; only needs to be non-NULL */
    .transmit = ppp_transmit,
};

/* serial -> netif.  Sole reader of the USJ port. */
static void rx_task(void *arg)
{
    (void)arg;
    static uint8_t buf[1024];
    for (;;) {
        int n = usb_serial_jtag_read_bytes(buf, sizeof(buf), pdMS_TO_TICKS(20));
        if (n > 0) {
            esp_netif_receive(s_netif, buf, n, NULL);
        }
    }
}

/* The lwIP netif behind a PPP esp_netif is point-to-point: it carries no MAC
 * and lwIP therefore leaves NETIF_FLAG_MLD6 / NETIF_FLAG_IGMP clear.  The
 * border router joins multicast groups on its backbone (mld6_joingroup_netif
 * from libopenthread_br.a) and mDNS needs IGMP, both of which refuse to work
 * on a netif without the flags — igmp_joingroup_netif() hard-errors on it.
 *
 * Setting the flags on a P2P link is safe because there is no MAC-level
 * multicast filter to program: every frame reaches the single peer regardless.
 * NOT yet verified on hardware — first thing to check when mDNS or RA/RS
 * misbehave (PLAN.md phase 3/4). */
static esp_err_t enable_multicast_flags(void *ctx)
{
    struct netif *lwip_netif = (struct netif *)ctx;
    lwip_netif->flags |= NETIF_FLAG_MLD6 | NETIF_FLAG_IGMP;
    return ESP_OK;
}

static void on_ip_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg; (void)base;

    if (id == IP_EVENT_PPP_GOT_IP) {
        ip_event_got_ip_t *e = (ip_event_got_ip_t *)data;
        s_peer_ipv4 = e->ip_info.gw.addr;

        struct netif *lwip_netif = esp_netif_get_netif_impl(s_netif);
        if (lwip_netif) {
            /* Touch lwIP state only from the TCP/IP thread. */
            esp_netif_tcpip_exec(enable_multicast_flags, lwip_netif);
        }

        ESP_LOGI(TAG, "up: local " IPSTR " peer " IPSTR,
                 IP2STR(&e->ip_info.ip), IP2STR(&e->ip_info.gw));

        /* esp_netif emits IP_EVENT_GOT_IP6 for WiFi/Ethernet netifs but not
         * for PPP — IPV6CP brings the link-local address up inside lwIP
         * without going through the esp_netif IPv6 path.  Anything that keys
         * off that event (the border-router library is the suspect here)
         * therefore never learns the backbone has IPv6.  Post it ourselves
         * and log whether it was already there.  EXPERIMENT — see PLAN.md. */
        esp_netif_ip6_info_t ip6 = {0};
        if (esp_netif_get_ip6_linklocal(s_netif, &ip6.ip) == ESP_OK) {
            ip_event_got_ip6_t ev6 = { .esp_netif = s_netif, .ip6_info = ip6,
                                       .ip_index = 0 };
            esp_err_t perr = esp_event_post(IP_EVENT, IP_EVENT_GOT_IP6, &ev6,
                                            sizeof(ev6), pdMS_TO_TICKS(100));
            ESP_LOGI(TAG, "synthesised GOT_IP6 for backbone: %s", esp_err_to_name(perr));
        } else {
            ESP_LOGW(TAG, "backbone has no link-local IPv6 yet");
        }

        xEventGroupSetBits(s_events, LINK_UP_BIT);

    } else if (id == IP_EVENT_GOT_IP6) {
        ip_event_got_ip6_t *e6 = (ip_event_got_ip6_t *)data;
        ESP_LOGI(TAG, "GOT_IP6 observed on netif %p (ours=%p)",
                 (void *)e6->esp_netif, (void *)s_netif);

    } else if (id == IP_EVENT_PPP_LOST_IP) {
        ESP_LOGW(TAG, "lost IP — renegotiating");
        xEventGroupClearBits(s_events, LINK_UP_BIT);
        s_peer_ipv4 = 0;
        esp_netif_action_start(s_netif, NULL, 0, NULL);
        esp_netif_action_connected(s_netif, NULL, 0, NULL);
    }
}

esp_err_t backbone_start(void)
{
    s_events = xEventGroupCreate();
    if (!s_events) {
        return ESP_ERR_NO_MEM;
    }

    usb_serial_jtag_driver_config_t ucfg = {
        .tx_buffer_size = 2048,
        .rx_buffer_size = 2048,
    };
    esp_err_t err = usb_serial_jtag_driver_install(&ucfg);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        ESP_LOGE(TAG, "USJ driver install failed: %s", esp_err_to_name(err));
        return err;
    }

    ESP_RETURN_ON_ERROR(esp_netif_init(), TAG, "esp_netif_init");
    err = esp_event_loop_create_default();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        return err;
    }
    ESP_RETURN_ON_ERROR(esp_event_handler_register(IP_EVENT, ESP_EVENT_ANY_ID,
                                                   on_ip_event, NULL),
                        TAG, "event_handler_register");

    esp_netif_inherent_config_t base = ESP_NETIF_INHERENT_DEFAULT_PPP();
    base.if_desc = "thbr_ppp";
    esp_netif_config_t cfg = {
        .base   = &base,
        .driver = &s_driver_cfg,
        .stack  = ESP_NETIF_NETSTACK_DEFAULT_PPP,
    };
    s_netif = esp_netif_new(&cfg);
    if (!s_netif) {
        ESP_LOGE(TAG, "esp_netif_new(PPP) failed");
        return ESP_FAIL;
    }

    if (xTaskCreate(rx_task, "ppp_rx", 4096, NULL, 12, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    esp_netif_action_start(s_netif, NULL, 0, NULL);
    esp_netif_action_connected(s_netif, NULL, 0, NULL);

    ESP_LOGI(TAG, "started (backend=USB-Serial/JTAG) — port now owned by PPP");
    return ESP_OK;
}

esp_err_t backbone_announce_got_ip(void)
{
    /* PPP posts IP_EVENT_PPP_GOT_IP on its own; nothing to synthesise. */
    return ESP_OK;
}

bool backbone_is_up(void)
{
    return s_events && (xEventGroupGetBits(s_events) & LINK_UP_BIT);
}

esp_err_t backbone_wait_up(uint32_t timeout_ms)
{
    if (!s_events) {
        return ESP_ERR_INVALID_STATE;
    }
    EventBits_t b = xEventGroupWaitBits(s_events, LINK_UP_BIT, pdFALSE, pdTRUE,
                                        pdMS_TO_TICKS(timeout_ms));
    return (b & LINK_UP_BIT) ? ESP_OK : ESP_ERR_TIMEOUT;
}

esp_netif_t *backbone_netif(void) { return s_netif; }

uint32_t backbone_peer_ipv4(void) { return s_peer_ipv4; }
