# Exactly one backbone transport is compiled (see main/backbone.h).
if(CONFIG_THBR_TRANSPORT_TAP)
    set(THBR_TRANSPORT_SRC "eth_link.c")
else()
    set(THBR_TRANSPORT_SRC "ppp_link.c")
endif()

idf_component_register(
    SRCS         "thbr_main.c" ${THBR_TRANSPORT_SRC} "log_sink.c" "p2p_netif_glue.c"
                 "info_server.c"
    INCLUDE_DIRS "."
    ## openthread   — OT stack, border-router API (prebuilt libopenthread_br.a)
    ## esp_netif    — the PPP backbone netif
    ## esp_driver_usb_serial_jtag — PPP's serial backend
    ## espressif__mdns — MeshCoP/SRP advertising on the backbone
    ## vfs          — esp_vfs_eventfd_register
    PRIV_REQUIRES openthread esp_netif esp_event esp_driver_usb_serial_jtag
                  espressif__mdns nvs_flash vfs lwip esp_hw_support spi_flash
                  spiffs espressif__esp_ot_br_server esp_http_server esp_timer
    ## WHOLE_ARCHIVE: __wrap_esp_netif_get_mac (p2p_netif_glue.c) is referenced
    ## only from libopenthread_br.a, which the linker processes AFTER libmain.a
    ## — without this the wrap ends up undefined at link time even though it is
    ## compiled.
    WHOLE_ARCHIVE
)

# Intercept esp_netif_get_mac so the point-to-point backbone can answer
# OpenThread's infra-interface link-layer-address query with a synthetic MAC.
# See main/p2p_netif_glue.c for why this is a link-time wrap and NOT an edit to
# the shared framework-espidf tree.
target_link_libraries(${COMPONENT_LIB} INTERFACE "-Wl,--wrap=esp_netif_get_mac")
