/*
 * OpenThread platform-config macros for the single-chip C6 border router.
 *
 * Trimmed from the IDF 6.0.1 ot_br example (esp_ot_config.h) to the
 * native-radio path only — the C6 runs the 802.15.4 radio itself, so there is
 * no UART/SPI spinel RCP variant to configure.
 *
 * SPDX-License-Identifier: CC0-1.0  (derived from Espressif CC0 example code)
 */

#pragma once
#include "esp_openthread_types.h"

#define ESP_OPENTHREAD_DEFAULT_RADIO_CONFIG() \
    {                                         \
        .radio_mode = RADIO_MODE_NATIVE,      \
    }

#define ESP_OPENTHREAD_DEFAULT_HOST_CONFIG()               \
    {                                                      \
        .host_connection_mode = HOST_CONNECTION_MODE_NONE, \
    }

#define ESP_OPENTHREAD_DEFAULT_PORT_CONFIG() \
    {                                        \
        .storage_partition_name = "nvs",     \
        .netif_queue_size = 10,              \
        .task_queue_size = 10,               \
    }
