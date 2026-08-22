/*
 * Ethernet-over-CDC-ACM backbone — the L2 counterpart to ppp_link.c.
 *
 * The C6's USB block is hardwired to CDC-ACM (TRM ch. 32), so an Ethernet USB
 * gadget is not an option: the frames go through the serial byte stream,
 * SLIP-framed, and a TAP device on the host puts them back on a real L2
 * segment (tools/tap_pump.py).
 *
 * The point is not the transport — PPP moved IP just fine.  The point is that
 * the netif this creates is an ESP_NETIF_NETSTACK_DEFAULT_ETH one: it has a
 * MAC, it has NETIF_FLAG_BROADCAST | NETIF_FLAG_ETHARP, and lwIP gives it the
 * MLD6/IGMP behaviour a point-to-point netif never gets.  That is what the
 * border-router library needs in order to initialise its infra-interface side.
 *
 * Addressing is static on both ends (no DHCP server on the stick side, and a
 * bench link with exactly two nodes does not need one).
 */

#include "backbone.h"

#include <stdlib.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"

#include "driver/usb_serial_jtag.h"
#include "esp_check.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_netif_net_stack.h"
#include "lwip/netif.h"

static const char *TAG = "eth_link";

#define LINK_UP_BIT   BIT0

/* RFC 1055 */
#define SLIP_END      0xC0
#define SLIP_ESC      0xDB
#define SLIP_ESC_END  0xDC
#define SLIP_ESC_ESC  0xDD

/* Ethernet header + MTU, plus slack.  Worst-case SLIP expansion is 2x, which
 * only affects the TX scratch buffer. */
#define ETH_FRAME_MAX (14 + CONFIG_THBR_TAP_MTU + 4)

static esp_netif_t       *s_netif;
static EventGroupHandle_t s_events;
static uint32_t           s_peer_ipv4;
static uint32_t           s_rx_frames, s_tx_frames, s_rx_overruns;

/* netif -> serial.  lwIP hands us a complete Ethernet frame; SLIP-frame it.
 *
 * Bounded write for the same reason as the PPP path: on USB-Serial/JTAG the TX
 * ring only drains while a host reads, so an unbounded write would wedge the
 * lwIP thread whenever the pump is not running.  A dropped frame is what
 * Ethernet is allowed to do; a wedged stack is not. */
static esp_err_t eth_transmit(void *h, void *buffer, size_t len)
{
    (void)h;
    static uint8_t enc[2 * ETH_FRAME_MAX + 2];
    const uint8_t *p = (const uint8_t *)buffer;
    size_t n = 0;

    if (len > ETH_FRAME_MAX) {
        return ESP_ERR_INVALID_SIZE;
    }

    enc[n++] = SLIP_END;
    for (size_t i = 0; i < len; i++) {
        if (p[i] == SLIP_END) {
            enc[n++] = SLIP_ESC;
            enc[n++] = SLIP_ESC_END;
        } else if (p[i] == SLIP_ESC) {
            enc[n++] = SLIP_ESC;
            enc[n++] = SLIP_ESC_ESC;
        } else {
            enc[n++] = p[i];
        }
    }
    enc[n++] = SLIP_END;

    usb_serial_jtag_write_bytes(enc, n, pdMS_TO_TICKS(500));
    s_tx_frames++;
    return ESP_OK;
}

/* The ETH netstack does NOT copy: esp_pbuf_allocate() wraps the buffer we hand
 * to esp_netif_receive() by reference and releases it later through this
 * callback (esp_netif/lwip/netif/ethernetif.c).  So every received frame needs
 * its own heap allocation — a reused static buffer would be rewritten while
 * lwIP still points at it. */
static void eth_free_rx(void *h, void *buffer)
{
    (void)h;
    free(buffer);
}

static const esp_netif_driver_ifconfig_t s_driver_cfg = {
    .handle                = (void *)1,
    .transmit              = eth_transmit,
    .driver_free_rx_buffer = eth_free_rx,
};

/* serial -> netif.  Sole reader of the port. */
static void rx_task(void *arg)
{
    (void)arg;
    static uint8_t rx[512];
    static uint8_t frame[ETH_FRAME_MAX];
    size_t flen = 0;
    bool esc = false;

    for (;;) {
        int n = usb_serial_jtag_read_bytes(rx, sizeof(rx), pdMS_TO_TICKS(20));
        for (int i = 0; i < n; i++) {
            uint8_t b = rx[i];

            if (b == SLIP_END) {
                /* Frames shorter than an Ethernet header are noise — the boot
                 * banner shares this port before the handover. */
                if (flen >= 14) {
                    void *owned = malloc(flen);
                    if (owned) {
                        memcpy(owned, frame, flen);
                        /* On any failure path ethernetif_input calls
                         * eth_free_rx itself, so ownership is handed over
                         * either way. */
                        esp_netif_receive(s_netif, owned, flen, NULL);
                        s_rx_frames++;
                    } else {
                        s_rx_overruns++;
                    }
                }
                flen = 0;
                esc = false;
                continue;
            }
            if (esc) {
                b = (b == SLIP_ESC_END) ? SLIP_END
                  : (b == SLIP_ESC_ESC) ? SLIP_ESC
                  : b;
                esc = false;
            } else if (b == SLIP_ESC) {
                esc = true;
                continue;
            }
            if (flen < sizeof(frame)) {
                frame[flen++] = b;
            } else {
                s_rx_overruns++;
                flen = 0;
                esc = false;
            }
        }
    }
}

/* lwIP clears NETIF_FLAG_MLD6 on netifs it does not consider multicast-capable
 * at init time; an ETH netstack netif gets BROADCAST|ETHARP|IGMP but MLD6
 * depends on LWIP_IPV6_MLD.  Assert both explicitly so mDNS and the border
 * router's mld6_joingroup_netif() have what they need. */
static esp_err_t enable_multicast_flags(void *ctx)
{
    struct netif *lwip_netif = (struct netif *)ctx;
    lwip_netif->flags |= NETIF_FLAG_MLD6 | NETIF_FLAG_IGMP | NETIF_FLAG_BROADCAST;
    return ESP_OK;
}

esp_err_t backbone_start(void)
{
    s_events = xEventGroupCreate();
    if (!s_events) {
        return ESP_ERR_NO_MEM;
    }

    usb_serial_jtag_driver_config_t ucfg = {
        .tx_buffer_size = 4096,
        .rx_buffer_size = 4096,
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

    /* An Ethernet netif, but without the DHCP client the default config turns
     * on: the peer is a bench TAP with a static address, not a network with a
     * server on it. */
    esp_netif_inherent_config_t base = ESP_NETIF_INHERENT_DEFAULT_ETH();
    base.if_desc = "thbr_tap";
    base.flags   = (esp_netif_flags_t)(ESP_NETIF_FLAG_AUTOUP);

    esp_netif_config_t cfg = {
        .base   = &base,
        .driver = &s_driver_cfg,
        .stack  = ESP_NETIF_NETSTACK_DEFAULT_ETH,
    };
    s_netif = esp_netif_new(&cfg);
    if (!s_netif) {
        ESP_LOGE(TAG, "esp_netif_new(ETH) failed");
        return ESP_FAIL;
    }

    /* A real, stable MAC — this is the whole point of the lane.  Base MAC with
     * the locally-administered bit set so it cannot collide with the chip's
     * own WiFi/15.4 addresses on the same segment. */
    uint8_t mac[6];
    ESP_RETURN_ON_ERROR(esp_read_mac(mac, ESP_MAC_BASE), TAG, "esp_read_mac");
    mac[0] = (uint8_t)((mac[0] | 0x02) & 0xFE);
    ESP_RETURN_ON_ERROR(esp_netif_set_mac(s_netif, mac), TAG, "esp_netif_set_mac");

    esp_netif_ip_info_t ip = {0};
    ip.ip.addr      = ipaddr_addr(CONFIG_THBR_TAP_STICK_IPV4);
    ip.gw.addr      = ipaddr_addr(CONFIG_THBR_TAP_HOST_IPV4);
    ip.netmask.addr = ipaddr_addr(CONFIG_THBR_TAP_NETMASK);
    s_peer_ipv4     = ip.gw.addr;

    esp_netif_dhcpc_stop(s_netif);   /* may already be stopped; ignore */
    ESP_RETURN_ON_ERROR(esp_netif_set_ip_info(s_netif, &ip), TAG, "set_ip_info");

    if (xTaskCreate(rx_task, "eth_rx", 4096, NULL, 12, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }

    esp_netif_action_start(s_netif, NULL, 0, NULL);
    /* There is no carrier to detect on a serial line: the link is up as soon
     * as we are. The pump on the other end may not be running yet, which looks
     * exactly like an unplugged cable — frames get dropped, nothing wedges. */
    esp_netif_action_connected(s_netif, NULL, 0, NULL);

    struct netif *lwip_netif = esp_netif_get_netif_impl(s_netif);
    if (lwip_netif) {
        esp_netif_tcpip_exec(enable_multicast_flags, lwip_netif);
    }
    ESP_RETURN_ON_ERROR(esp_netif_create_ip6_linklocal(s_netif), TAG, "ip6_linklocal");

    xEventGroupSetBits(s_events, LINK_UP_BIT);

    ESP_LOGI(TAG, "backbone up: ETH over CDC-ACM (SLIP), mac %02x:%02x:%02x:%02x:%02x:%02x, "
                  "ip " IPSTR " peer " IPSTR,
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5],
             IP2STR(&ip.ip), IP2STR(&ip.gw));
    return ESP_OK;
}

esp_err_t backbone_announce_got_ip(void)
{
    if (!s_netif) {
        return ESP_ERR_INVALID_STATE;
    }
    esp_netif_ip_info_t ip = {0};
    ESP_RETURN_ON_ERROR(esp_netif_get_ip_info(s_netif, &ip), TAG, "get_ip_info");

    ip_event_got_ip_t evt = {
        .esp_netif  = s_netif,
        .ip_info    = ip,
        .ip_changed = true,
    };
    return esp_event_post(IP_EVENT, IP_EVENT_ETH_GOT_IP, &evt, sizeof(evt),
                          pdMS_TO_TICKS(100));
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
