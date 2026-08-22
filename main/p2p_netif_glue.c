/*
 * Give the point-to-point backbone netif a link-layer address.
 *
 * WHY THIS EXISTS
 * ---------------
 * OpenThread asks its infrastructure interface for a link-layer address and
 * puts it into the Source Link-Layer Address option of the Router
 * Advertisements it emits (core/border_router/routing_manager.cpp,
 * rx_ra_tracker.cpp).  IDF answers that request in
 * components/openthread/src/esp_openthread_netif_glue.c:
 *
 *     otPlatGetInfraIfLinkLayerAddress(...)
 *         esp_netif_get_mac(backbone_netif, aInfraIfLinkLayerAddress->mAddress);
 *         aInfraIfLinkLayerAddress->mLength = 6;
 *
 * — without checking the return value.  On a PPP (or TUN) netif
 * esp_netif_get_mac() returns ESP_ERR_NOT_SUPPORTED without touching the
 * buffer (esp_netif_lwip.c, _IS_NETIF_ANY_POINT2POINT_TYPE), so OpenThread
 * would advertise six bytes of uninitialised stack.
 *
 * HOW
 * ---
 * We intercept esp_netif_get_mac with the linker's --wrap (see
 * main/CMakeLists.txt) and answer for point-to-point netifs with a stable,
 * locally-administered address derived from the chip's base MAC.  Everything
 * else is passed through untouched.
 *
 * Deliberately NOT a patch to the IDF tree: several projects on this host
 * share one framework-espidf checkout (TBR builds the border router against
 * the very same tree), so a tree edit would leak across projects.
 */

#include <string.h>

#include "esp_check.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"

static const char *TAG = "p2p_glue";

esp_err_t __real_esp_netif_get_mac(esp_netif_t *esp_netif, uint8_t mac[]);

esp_err_t __wrap_esp_netif_get_mac(esp_netif_t *esp_netif, uint8_t mac[])
{
    esp_err_t err = __real_esp_netif_get_mac(esp_netif, mac);
    if (err != ESP_ERR_NOT_SUPPORTED) {
        return err;   /* a real netif answered — nothing to do */
    }

    /* Point-to-point netif: synthesise one.  Base MAC with the
     * locally-administered bit set and the multicast bit clear, so it can
     * never collide with a real vendor address.
     *
     * NOTE esp_read_mac(..., ESP_MAC_BASE) — not esp_efuse_mac_get_default(),
     * which on C6/H2 hands back the EUI-64 form, with ff:fe inserted in the
     * middle.  esptool prints both, as MAC and BASE MAC; only the latter is
     * the 48-bit address everything else refers to. */
    uint8_t base[6];
    ESP_RETURN_ON_ERROR(esp_read_mac(base, ESP_MAC_BASE), TAG, "esp_read_mac");

    base[0] = (uint8_t)((base[0] | 0x02) & 0xFE);
    memcpy(mac, base, sizeof(base));

    static bool logged;
    if (!logged) {
        logged = true;
        ESP_LOGI(TAG, "synthesised P2P link-layer address %02x:%02x:%02x:%02x:%02x:%02x",
                 mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    }
    return ESP_OK;
}
