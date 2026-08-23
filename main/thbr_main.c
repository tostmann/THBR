/*
 * THBR — ESP32-C6 single-chip Thread Border Router whose IPv6 backbone is a
 * serial IP link (PPP over the native USB-Serial/JTAG port), not WiFi and not
 * Ethernet.
 *
 * Rationale, measurements and the HAOS/TAP product lane: PLAN.md and
 * ~/.claude/docs/ppp-ncp-transport-busware.md.  Sibling project TBR does the
 * same job with a W5500 Ethernet backbone and is the source of this control
 * flow.
 *
 * THE PORT HANDOVER
 * -----------------
 * This chip has exactly one USB port and PPP needs it.  Boot therefore runs in
 * two acts:
 *
 *   act 1  console on USB-Serial/JTAG, plain text, THBR_CONSOLE_GRACE_MS long
 *          — this is what answers "did it boot at all?" without a UART adapter
 *   act 2  console detached, PPP owns the port, logs continue as UDP datagrams
 *          to the PPP peer (see log_sink.h; on the host: nc -u -l -p 5514)
 *
 * Verified ordering note (inherited from TBR, 2026-05-28):
 * esp_openthread_border_router.h claims set_backbone_netif() must be called
 * before esp_openthread_init().  Espressif's own current example calls it
 * AFTER esp_openthread_start() under the OT lock — we follow the example.
 */

#include <stdio.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "esp_chip_info.h"
#include "esp_check.h"
#include "esp_err.h"
#include "esp_event.h"
#include "esp_flash.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_netif_net_stack.h"
#include "lwip/netif.h"
#include "esp_vfs_eventfd.h"
#include "nvs_flash.h"

#if CONFIG_THBR_ENABLE_BORDER_ROUTER
#include "mdns.h"

void thbr_ble_proxy_start(const char *uri);
#include "esp_openthread.h"
#include "esp_openthread_border_router.h"
#include "esp_openthread_lock.h"
#include "esp_openthread_netif_glue.h"
#include "openthread/border_routing.h"
#include "openthread/dataset.h"
#include "openthread/ip6.h"
#include "openthread/thread.h"
#include "openthread/platform/infra_if.h"
#include "esp_openthread_types.h"
#include "esp_ot_config.h"
#include "esp_br_web.h"      /* esp_ot_br_server: REST API + web GUI */
#include "esp_spiffs.h"
#endif

#include "esp_task_wdt.h"
#include "log_sink.h"
#include "info_server.h"
#include "backbone.h"
#include "version.h"

#define TAG "thbr"

static void banner(void)
{
    esp_chip_info_t chip;
    esp_chip_info(&chip);

    uint32_t flash_size = 0;
    esp_flash_get_size(NULL, &flash_size);

    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_BASE);

    printf("\n=== THBR v%s (%s) ===\n", FW_VERSION_STRING, FW_BUILD_DATE);
    #if CONFIG_THBR_TRANSPORT_TAP
    printf("On-chip Thread Border Router, backbone = Ethernet over CDC-ACM (host TAP)\n");
#else
    printf("On-chip Thread Border Router, backbone = PPP over USB-Serial/JTAG\n");
#endif
    printf("chip: C6 rev v%d.%d, %" PRIu32 " MB flash, base MAC %02x:%02x:%02x:%02x:%02x:%02x\n",
           chip.revision / 100, chip.revision % 100, flash_size / (1024 * 1024),
           mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    printf("free heap: %" PRIu32 " bytes\n\n", esp_get_free_heap_size());
}

#if CONFIG_THBR_ENABLE_BORDER_ROUTER
/* Wait for the backbone to carry IP, then start the border-router role bound
 * to it.  Own task: the link wait blocks. */
static void border_router_init_task(void *ctx)
{
    (void)ctx;

    esp_err_t err = backbone_wait_up(CONFIG_THBR_BACKBONE_WAIT_MS);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "backbone not up after %d ms — initialising BR anyway; "
                      "border routing starts when PPP negotiates",
                 CONFIG_THBR_BACKBONE_WAIT_MS);
    }

    esp_openthread_lock_acquire(portMAX_DELAY);
    esp_openthread_set_backbone_netif(backbone_netif());
    err = esp_openthread_border_router_init();

    if (err != ESP_OK) {
        /* This is the project's go/no-go point: libopenthread_br.a is a
         * prebuilt blob, so a failure here means it holds an assumption about
         * the backbone that a point-to-point netif cannot satisfy.  Do not
         * dig into the blob — switch to the TAP lane (PLAN.md phase 3). */
        ESP_LOGE(TAG, "esp_openthread_border_router_init failed: %s", esp_err_to_name(err));
        esp_openthread_lock_release();
        vTaskDelete(NULL);
        return;
    }
    ESP_LOGI(TAG, "border router initialised — backbone = PPP netif");
    info_server_set_ot_ready(true);

    /* Bring the Thread network up.  The serial CLI is not available (PPP owns
     * the port), so the firmware forms or rejoins the network itself: reuse
     * the dataset in NVS if there is one, otherwise create a network.  Until
     * Thread is running the border routing manager has nothing to advertise
     * and the backbone stays silent. */
    otOperationalDatasetTlvs dataset;
    otError oterr = otDatasetGetActiveTlvs(esp_openthread_get_instance(), &dataset);
    err = esp_openthread_auto_start((oterr == OT_ERROR_NONE) ? &dataset : NULL);
    ESP_LOGI(TAG, "thread network %s: %s",
             (oterr == OT_ERROR_NONE) ? "resumed from NVS" : "created",
             esp_err_to_name(err));

    /* Dump the active dataset as TLV hex.  Without a CLI this is the only way
     * to learn the network credentials, and it is exactly what a second node
     * needs in order to join:  ot_cli> dataset set active <hex> */
    {
        otOperationalDatasetTlvs tlvs;
        if (otDatasetGetActiveTlvs(esp_openthread_get_instance(), &tlvs) == OT_ERROR_NONE) {
            char hex[2 * OT_OPERATIONAL_DATASET_MAX_LENGTH + 1];
            for (uint8_t i = 0; i < tlvs.mLength; i++) {
                snprintf(&hex[2 * i], 3, "%02x", tlvs.mTlvs[i]);
            }
            hex[2 * tlvs.mLength] = '\0';
            ESP_LOGI(TAG, "active dataset tlvs: %s", hex);
        }
    }

    /* Tell the OT core the infrastructure link is running — at the index the
     * LIBRARY holds, which is not necessarily the netif's.
     *
     * Measured on one deployment: the backbone netif is index 3
     * (and no netif with a higher index exists), but libopenthread_br.a had
     * initialised its InfraIf with index 7.  Every state report against index
     * 3 is therefore rejected with OT_ERROR_INVALID_ARGS, the library keeps
     * believing its infrastructure link is down, and the routing manager stays
     * 'stopped' forever — border routing never starts, on a host where
     * everything else looks identical to the working bench.
     *
     * Rather than guess where the 7 comes from (the library is a binary), find
     * the index empirically: infra_if.cpp:172-173 answers INVALID_ARGS for a
     * wrong index and something else for the right one.  The call is
     * idempotent, so probing costs nothing when the index already matches. */
    uint32_t netif_idx = esp_netif_get_netif_impl_index(backbone_netif());
    uint32_t infra_idx = netif_idx;
    otError ierr = OT_ERROR_INVALID_ARGS;
    for (uint32_t idx = 0; idx <= 20; idx++) {
        otError e = otPlatInfraIfStateChanged(esp_openthread_get_instance(), idx, true);
        if (e != OT_ERROR_INVALID_ARGS) {
            infra_idx = idx;
            ierr = e;
            break;
        }
    }
    ESP_LOGI(TAG, "infra-if reported running: netif_idx=%" PRIu32 " library_idx=%" PRIu32
                  "%s ot_error=%d br_state=%d",
             netif_idx, infra_idx,
             (netif_idx == infra_idx) ? "" : "  <-- MISMATCH (library disagrees)",
             (int)ierr, (int)otBorderRoutingGetState(esp_openthread_get_instance()));
    otIp6Prefix onlink;
    bool have_onlink =
        (otBorderRoutingGetOnLinkPrefix(esp_openthread_get_instance(), &onlink) == OT_ERROR_NONE);
    esp_openthread_lock_release();

    /* Give the backbone an address out of the on-link prefix the routing
     * manager advertises there.
     *
     * WHY: traffic from the mesh towards the infrastructure was dropped —
     * nothing reached the host, while mesh-internal pings worked.  The
     * suspicion is that lwIP cannot know the on-link prefix is reachable over
     * the backbone: the border router advertises it but holds no address in
     * it, so ip6_route() finds no interface and the forwarded packet dies.
     * Owning an address in the prefix makes it on-link for lwIP.
     *
     * MUST run with the OpenThread lock RELEASED: esp_netif_add_ip6_address()
     * dispatches to the TCP/IP thread and waits for it, and that thread takes
     * the OpenThread lock — holding it here deadlocks the whole stack (the
     * device stopped answering even ARP; measured 2026-08-20). */
    if (have_onlink) {
        esp_ip6_addr_t addr = {0};
        memcpy(addr.addr, onlink.mPrefix.mFields.m8, 8);      /* prefix /64 */
        uint8_t mac[6];
        esp_read_mac(mac, ESP_MAC_BASE);
        uint8_t *iid = ((uint8_t *)addr.addr) + 8;            /* modified EUI-64 */
        iid[0] = (uint8_t)((mac[0] | 0x02) & 0xFE);
        iid[1] = mac[1]; iid[2] = mac[2];
        iid[3] = 0xff;   iid[4] = 0xfe;
        iid[5] = mac[3]; iid[6] = mac[4]; iid[7] = mac[5];

        esp_err_t aerr = esp_netif_add_ip6_address(backbone_netif(), addr, true);
        const uint8_t *b = (const uint8_t *)addr.addr;
        ESP_LOGI(TAG, "backbone on-link addr %02x%02x:%02x%02x:%02x%02x:%02x%02x:"
                      "%02x%02x:%02x%02x:%02x%02x:%02x%02x -> %s",
                 b[0],b[1],b[2],b[3],b[4],b[5],b[6],b[7],
                 b[8],b[9],b[10],b[11],b[12],b[13],b[14],b[15],
                 esp_err_to_name(aerr));
    } else {
        ESP_LOGW(TAG, "no on-link prefix from the routing manager yet");
    }

    /* ot-br-posix-compatible REST API (+ web GUI) on port 80.  This is what
     * turns "Home Assistant can see the router" into "Home Assistant can use
     * it": /node/dataset/active is how it imports the network credentials and
     * commissions Matter-over-Thread devices.  Started after border routing is
     * up so the endpoints answer with real state. */
    esp_br_web_start("/spiffs");
    /* esp_br_web_start only registers GOT_IP handlers — the HTTP server comes
     * up inside them.  Our static backbone never posts such an event, so
     * announce it now that the handlers exist. */
    esp_err_t werr = backbone_announce_got_ip();
    ESP_LOGI(TAG, "REST API + web GUI: announced backbone address -> %s",
             esp_err_to_name(werr));

    vTaskDelete(NULL);
}

/* Dump every lwIP netif on the chip, with the values that decide whether the
 * border-router library considers its infrastructure interface usable.
 *
 * Why this exists: identical firmware reports br=running on one host and
 * br=stopped on another, with the Thread network ruled out as the cause.  The
 * only remaining difference must be visible in the netif table — how many
 * interfaces exist, which index the backbone got, and what flags it carries.
 * Note the border-router library creates a netif of its own
 * (esp_openthread_border_router_second_netif_init), so the numbering is not
 * necessarily the same on both hosts.
 *
 * Runs in the TCP/IP thread: the netif list must not be walked from elsewhere.
 */
static esp_err_t dump_netifs(void *ctx)
{
    struct netif *backbone = (struct netif *)ctx;
    struct netif *n;

    NETIF_FOREACH(n) {
        ESP_LOGW(TAG, "netif %c%c%d idx=%u flags=0x%02x up=%d link=%d mtu=%d%s",
                 n->name[0] ? n->name[0] : '?', n->name[1] ? n->name[1] : '?',
                 n->num, (unsigned)netif_get_index(n), n->flags,
                 (int)netif_is_up(n), (int)netif_is_link_up(n), n->mtu,
                 (n == backbone) ? "  <-- BACKBONE" : "");
    }
    return ESP_OK;
}

static void log_netif_table(void)
{
    struct netif *bb = backbone_netif() ? esp_netif_get_netif_impl(backbone_netif()) : NULL;
    esp_netif_tcpip_exec(dump_netifs, bb);
}
#endif /* CONFIG_THBR_ENABLE_BORDER_ROUTER */

void app_main(void)
{
    /* --- act 1: plain console ------------------------------------------- */
    banner();

    ESP_ERROR_CHECK(nvs_flash_init());
    ESP_ERROR_CHECK(log_sink_init());

    /* eventfds: netif + task queue + border router, plus the native 802.15.4
     * radio driver. */
    esp_vfs_eventfd_config_t eventfd_config = { .max_fds = 4 };
    ESP_ERROR_CHECK(esp_vfs_eventfd_register(&eventfd_config));

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    /* Everything that does NOT need the backbone netif runs while the console
     * is still readable.  Anything that aborts here says so in plain text on
     * the USB port; once PPP owns it, esp_rom_printf output (which is what
     * ESP_ERROR_CHECK uses) disappears into the driver's ring buffer. */
#if CONFIG_THBR_ENABLE_BORDER_ROUTER
    /* Frontend assets for the web GUI.  A failed mount is not fatal — the REST
     * API, which is the part Home Assistant needs, works without them. */
    esp_vfs_spiffs_conf_t web_conf = {
        .base_path = "/spiffs",
        .partition_label = "web_storage",
        .max_files = 10,
        .format_if_mount_failed = false,
    };
    esp_err_t serr = esp_vfs_spiffs_register(&web_conf);
    printf("web_storage mount: %s\n", esp_err_to_name(serr));

    ESP_ERROR_CHECK(mdns_init());
    ESP_ERROR_CHECK(mdns_hostname_set("thbr"));

    static esp_openthread_config_t config = {
        .netif_config = ESP_NETIF_DEFAULT_OPENTHREAD(),
        .platform_config = {
            .radio_config = ESP_OPENTHREAD_DEFAULT_RADIO_CONFIG(),
            .host_config  = ESP_OPENTHREAD_DEFAULT_HOST_CONFIG(),
            .port_config  = ESP_OPENTHREAD_DEFAULT_PORT_CONFIG(),
        },
    };
    ESP_ERROR_CHECK(esp_openthread_start(&config));
    printf("OpenThread stack started\n");
#endif

    /* Bring the backbone up while the console still works, and report what it
     * produced.  After the handover nothing here is observable: the only port
     * is the backbone's, and ESP_ERROR_CHECK writes through esp_rom_printf
     * into the driver's ring buffer where it dies.  Starting the transport
     * only claims the port for frames — plain text still gets out until we
     * stop writing it. */
    esp_err_t berr = backbone_start();
    printf("backbone_start: %s\n", esp_err_to_name(berr));
    if (backbone_netif()) {
        struct netif *ln = esp_netif_get_netif_impl(backbone_netif());
        printf("backbone netif: esp_up=%d lwip_up=%d link_up=%d flags=0x%02x mtu=%d name=%c%c%d\n",
               esp_netif_is_netif_up(backbone_netif()),
               ln ? (int)netif_is_up(ln) : -1,
               ln ? (int)netif_is_link_up(ln) : -1,
               ln ? ln->flags : 0, ln ? ln->mtu : 0,
               ln ? ln->name[0] : '?', ln ? ln->name[1] : '?', ln ? ln->num : -1);
    }
    fflush(stdout);

    printf("handing the USB port to the backbone in %d ms — further logs go to UDP:%d "
           "on the host\n\n",
           CONFIG_THBR_CONSOLE_GRACE_MS, CONFIG_THBR_LOG_UDP_PORT);
    fflush(stdout);
    vTaskDelay(pdMS_TO_TICKS(CONFIG_THBR_CONSOLE_GRACE_MS));

    /* --- act 2: the backbone owns the port ------------------------------ */
    log_sink_detach_console();
    ESP_ERROR_CHECK(berr);
    ESP_ERROR_CHECK(log_sink_start_udp(0, CONFIG_THBR_LOG_UDP_PORT));
    /* Firmware identity for the host container — up before the border
     * router, so "THBR present, BR broken" is distinguishable from "no
     * THBR at all". */
    info_server_start();

#if CONFIG_THBR_ENABLE_BORDER_ROUTER
    /* From here on failures are logged, never fatal: aborting after the
     * handover would take the link down with it and leave nothing to debug
     * through.  The backbone is not a netif the mdns component discovers on
     * its own, so register it explicitly. */
    esp_err_t merr = mdns_register_netif(backbone_netif());
    if (merr != ESP_OK) {
        ESP_LOGE(TAG, "mdns_register_netif failed: %s", esp_err_to_name(merr));
    } else {
        merr = mdns_netif_action(backbone_netif(),
                                 MDNS_EVENT_ENABLE_IP4 | MDNS_EVENT_ENABLE_IP6 |
                                 MDNS_EVENT_ANNOUNCE_IP4 | MDNS_EVENT_ANNOUNCE_IP6);
        ESP_LOGI(TAG, "mdns on backbone: %s", esp_err_to_name(merr));
    }

    xTaskCreate(border_router_init_task, "thbr_br_init", 6144, NULL, 4, NULL);
#else
    ESP_LOGI(TAG, "stage 1 build: PPP backbone only, border router disabled "
                  "(CONFIG_THBR_ENABLE_BORDER_ROUTER=n)");
#endif

    /* Heartbeat so the UDP log shows liveness and the heap trend — the heap
     * headroom is one of the open questions for BR + mDNS + PPP on 512 KB.
     * On a border-router build it also reports the routing state, which is
     * what tells apart "initialised" from "actually advertising". */
    /* Watch this loop with the task watchdog.  A crash already reboots the
     * chip (panic handler), but a task that simply stops running does not —
     * and that is the failure that looks like a healthy border router from the
     * outside.  Only this task is watched: the idle tasks are deliberately not,
     * so a busy radio stack cannot trigger a reboot. */
    thbr_ble_proxy_start(CONFIG_THBR_BLE_PROXY_URI);  /* leer ohne NimBLE */

    esp_err_t werr = esp_task_wdt_add(NULL);
    ESP_LOGI(TAG, "heartbeat registered with the task watchdog: %s", esp_err_to_name(werr));

    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(10000));
        esp_task_wdt_reset();
#if CONFIG_THBR_ENABLE_BORDER_ROUTER
        static const char *br_state[] = { "uninitialized", "disabled", "stopped", "running" };
        static const char *role[] = { "disabled", "detached", "child", "router", "leader" };
        esp_openthread_lock_acquire(portMAX_DELAY);
        otInstance *inst = esp_openthread_get_instance();
        otBorderRoutingState brs = otBorderRoutingGetState(inst);
        otDeviceRole r = otThreadGetDeviceRole(inst);
        esp_openthread_lock_release();
        /* Say something every minute, and immediately whenever the state
         * changes.  At one line every ten seconds this heartbeat pushed the
         * host's own messages out of the add-on log window, which cost a
         * diagnosis: after a replug it was no longer possible to tell which
         * recovery path had run. */
        bool say;
        {
            static int beat;
            static otBorderRoutingState last_brs = (otBorderRoutingState)-1;
            static otDeviceRole last_role = (otDeviceRole)-1;
            static bool last_link;
            bool link_now = backbone_is_up();
            bool changed = (brs != last_brs) || (r != last_role) || (link_now != last_link);
            last_brs = brs; last_role = r; last_link = link_now;
            say = changed || ((++beat % 6) == 0);
        }
        if (say)
        ESP_LOGI(TAG, "alive: link=%s heap=%" PRIu32 " br=%s role=%s infra_idx=%d",
                 backbone_is_up() ? "up" : "down", esp_get_free_heap_size(),
                 (brs < 4) ? br_state[brs] : "?", (r < 5) ? role[r] : "?",
                 esp_netif_get_netif_impl_index(backbone_netif()));

        /* Report the mesh-side addresses once routing is live: without a CLI
         * this is the only way to learn the OMR address to ping from the
         * host, and it is what proves the route the host learned by RA
         * actually leads somewhere. */
        /* When border routing is NOT running, say WHY — otherwise the state is
         * a black box, which is exactly what once cost a migration its
         * diagnosis.  Two things are worth knowing:
         *
         *  - which ifIndex the border-router library actually holds.  Probing
         *    otPlatInfraIfStateChanged() across a small range identifies it:
         *    a wrong index answers OT_ERROR_INVALID_ARGS (7), the right one
         *    does not (infra_if.cpp:172-173).  Note the call is idempotent —
         *    if the state already matches it returns NONE and changes nothing.
         *  - whether the routing manager has prefixes at all.
         */
        if (brs != OT_BORDER_ROUTING_STATE_RUNNING) {
            esp_openthread_lock_acquire(portMAX_DELAY);
            otInstance *di = esp_openthread_get_instance();
            char probe[200];
            int n = 0;
            for (uint32_t idx = 0; idx <= 20 && n < (int)sizeof(probe) - 8; idx++) {
                n += snprintf(&probe[n], sizeof(probe) - n, "%" PRIu32 ":%d ",
                              idx, (int)otPlatInfraIfStateChanged(di, idx, true));
            }
            otIp6Prefix pfx;
            int e_onlink = (int)otBorderRoutingGetOnLinkPrefix(di, &pfx);
            int e_omr    = (int)otBorderRoutingGetOmrPrefix(di, &pfx);
            esp_openthread_lock_release();
            ESP_LOGW(TAG, "br not running: infra-probe [%s] (7=INVALID_ARGS=wrong idx) "
                          "onlink_err=%d omr_err=%d", probe, e_onlink, e_omr);
            /* The probe above already reported 'running' at whichever index the
             * library accepts, so this doubles as the recovery path: if the
             * mismatch was the only thing holding the routing manager back, it
             * starts on the next evaluation. */
        }

        /* The netif table is the last unexplored difference between the two
         * hosts — log it early (healthy reference) and whenever routing is
         * down (the failing case), so both can be compared side by side. */
        static int table_dumps;
        if (table_dumps < 3 || brs != OT_BORDER_ROUTING_STATE_RUNNING) {
            if (table_dumps < 40) {          /* never spam forever */
                table_dumps++;
                log_netif_table();
            }
        }

        /* Keep reporting until an address outside the mesh-local and
         * link-local prefixes shows up — that is the OMR address, the one an
         * off-mesh host can actually reach, and it only appears once the
         * routing manager has published its prefix into the mesh. */
        static bool omr_seen;
        if (!omr_seen && brs == OT_BORDER_ROUTING_STATE_RUNNING) {
            esp_openthread_lock_acquire(portMAX_DELAY);
            for (const otNetifAddress *a = otIp6GetUnicastAddresses(esp_openthread_get_instance());
                 a != NULL; a = a->mNext) {
                const uint8_t *b = a->mAddress.mFields.m8;
                bool mesh_local = (b[0] == 0xfd && b[1] == 0x00 && b[2] == 0x0d && b[3] == 0xb8);
                bool link_local = (b[0] == 0xfe && b[1] == 0x80);
                if (!mesh_local && !link_local) {
                    omr_seen = true;
                }
                ESP_LOGI(TAG, "thread addr: %02x%02x:%02x%02x:%02x%02x:%02x%02x:"
                              "%02x%02x:%02x%02x:%02x%02x:%02x%02x/%d%s%s",
                         b[0],b[1],b[2],b[3],b[4],b[5],b[6],b[7],
                         b[8],b[9],b[10],b[11],b[12],b[13],b[14],b[15],
                         a->mPrefixLength, a->mPreferred ? " (preferred)" : "",
                         (!mesh_local && !link_local) ? " <-- OMR, reachable from the host" : "");
            }
            esp_openthread_lock_release();
        }
#else
        ESP_LOGI(TAG, "alive: link=%s heap=%" PRIu32 " min=%" PRIu32,
                 backbone_is_up() ? "up" : "down",
                 esp_get_free_heap_size(), esp_get_minimum_free_heap_size());
#endif
    }
}
