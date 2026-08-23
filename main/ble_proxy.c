/* BLE Proxy for the Open Home Foundation Matter Server.
 *
 * A factory-new Matter device speaks only BLE, so commissioning needs a
 * Bluetooth radio.  The Matter server does not insist on having one itself:
 * it specifies a proxy protocol (docs/ble-proxy-protocol.md in
 * matter-js/matterjs-server, version 1) and drives someone else's radio over a
 * WebSocket.  This chip has one, and it is already on the host's network.
 *
 * So the whole Matter protocol stays where it belongs — in the certified
 * server — and what runs here is a radio driven from outside.  For a host with
 * no Bluetooth at all, that is the difference between being able to commission
 * a device and not.
 *
 * The proxy is the CLIENT: it dials ws://<host>:<port>/ble and waits to be
 * told what to do.
 *
 * Implements protocol version 1: the handshake, scanning, and the GATT half
 * that a commissioning run actually uses — connect, discovery, read, write,
 * subscribe — including the binary frames that carry the BTP stream, because
 * routing every one of those through JSON would be slow and pointless.
 */
#include "sdkconfig.h"

#ifdef CONFIG_BT_NIMBLE_ENABLED

#include <string.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_event.h"
#include "esp_websocket_client.h"
#include "cJSON.h"
#include "mbedtls/base64.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "host/ble_hs.h"
#include "host/util/util.h"

static const char *TAG = "ble_proxy";

#define PROTOCOL_VERSION 1

#define MAX_CONN      2
#define MAX_CHR      12
#define MAX_SVC      12

/* What a peripheral looks like from here.  The table of characteristics is
 * filled by discover_characteristics and then used by everything that names a
 * characteristic by UUID — the server never sees a handle. */
typedef struct {
    bool     in_use;
    uint16_t conn;             /* NimBLE connection handle */
    uint16_t proxy;            /* the handle we handed to the server */
    uint16_t mtu;
    uint8_t  addr[6];
    struct {
        ble_uuid_any_t uuid;
        uint16_t val_handle;
        uint16_t end_handle;
        uint16_t cccd;          /* pre-discovered, see on_disc_chr */
        uint8_t  props;
    } chr[MAX_CHR];
    int      n_chr;
    struct {
        ble_uuid_any_t uuid;
        uint16_t start, end;
    } svc[MAX_SVC];
    int      n_svc;
    uint16_t svc_start, svc_end;   /* range the characteristics came from */
    uint16_t write_target;         /* last write target, for binary frames */
    uint16_t notify_target;        /* last subscribed, for notifications */
} conn_t;

static conn_t s_conn[MAX_CONN];
static uint16_t s_next_proxy_handle = 1;

/* GATT in NimBLE is asynchronous, and the server sends one command at a time
 * and waits for the answer — so a single slot for the operation in flight is
 * enough, and far easier to reason about than a queue. */
typedef enum {
    OP_NONE = 0, OP_CONNECT, OP_DISC_SVC, OP_DISC_CHR, OP_DISC_DSC_MAP, OP_READ,
    OP_WRITE, OP_SUB_FIND_CCCD, OP_SUB_WRITE_CCCD, OP_WS_WRITE, OP_MTU,
    OP_CONNECT_MTU
} op_kind_t;

static struct {
    op_kind_t kind;
    int       id;
    conn_t   *c;
    cJSON    *acc;              /* collected results while discovering */
    uint16_t  cccd;
    uint16_t  target;           /* characteristic value handle in play */
    bool      subscribe_after;  /* write_and_subscribe: subscribe once written */
    ble_uuid_any_t sub_uuid;
    bool      binary_read;      /* answer a read as a binary frame */
} s_op;

/* Everything that leaves this chip goes through one queue and one task.
 *
 * Not a matter of taste: the websocket component states four times in its own
 * header that its send, start, stop and close functions "cannot be called from
 * the websocket event handler" — and answering a command straight from the
 * handler is exactly what this used to do.  It survived short exchanges and
 * wedged the moment a commissioning run produced replies and BTP frames back
 * to back: no answers, no reconnect, no crash.  The BLE callbacks had the same
 * problem from the other side, blocking the NimBLE host task while the radio
 * had work to do.
 *
 * So: the handler only copies frames in, the BLE callbacks only queue results,
 * and a single task owns the socket.
 */
#define Q_DEPTH 24

typedef enum { MSG_IN_TEXT, MSG_IN_BIN, MSG_OUT_TEXT, MSG_OUT_BIN } msg_kind_t;

typedef struct {
    msg_kind_t kind;
    uint8_t   *data;      /* malloc'd by the producer, freed by the task */
    size_t     len;
    uint8_t    opcode;    /* MSG_OUT_BIN */
    uint16_t   handle;    /* MSG_OUT_BIN */
} msg_t;

static QueueHandle_t s_q;

static void q_put(msg_t *m)
{
    if (!s_q || xQueueSend(s_q, m, 0) != pdTRUE) {
        /* Dropping is the lesser evil: blocking here would stall either the
         * radio or the socket, which is what this queue exists to prevent. */
        ESP_LOGW(TAG, "queue full, dropping a message");
        free(m->data);
    }
}

static esp_websocket_client_handle_t s_ws;
static bool s_scanning;
static bool s_was_connected;      /* to report a lost link, not a missing one */
static int64_t s_quiet_since;     /* when the last "no server" line went out */
static char s_uri[128];
static uint8_t s_own_addr_type;

/* ------------------------------------------------------------------ senden */

static void ws_send_json(cJSON *obj)
{
    char *txt = cJSON_PrintUnformatted(obj);
    cJSON_Delete(obj);
    if (!txt) return;
    size_t n = strlen(txt);
    uint8_t *copy = malloc(n);
    if (copy) {
        memcpy(copy, txt, n);
        msg_t m = { .kind = MSG_OUT_TEXT, .data = copy, .len = n };
        q_put(&m);
    }
    cJSON_free(txt);
}

static void reply_ok(int id, cJSON *result /* takes ownership, may be NULL */)
{
    cJSON *o = cJSON_CreateObject();
    cJSON_AddNumberToObject(o, "id", id);
    cJSON_AddBoolToObject(o, "success", true);
    cJSON_AddItemToObject(o, "result", result ? result : cJSON_CreateObject());
    ws_send_json(o);
}

static void reply_err_at(int id, const char *code, const char *msg, bool loud)
{
    /* A refused command used to look exactly like one that never arrived: the
     * server hung up and nothing was written here.  So the reason is logged —
     * except for the refusals that mean nothing is wrong.  Stopping a scan
     * that already stopped is one of those: the server does it at the end of
     * every run, and a warning for it trains the reader to ignore warnings. */
    if (loud) ESP_LOGW(TAG, "command %d refused: %s (%s)", id, code, msg);
    else      ESP_LOGD(TAG, "command %d declined: %s (%s)", id, code, msg);
    cJSON *o = cJSON_CreateObject();
    cJSON_AddNumberToObject(o, "id", id);
    cJSON_AddBoolToObject(o, "success", false);
    cJSON_AddStringToObject(o, "error", code);
    cJSON_AddStringToObject(o, "message", msg);
    ws_send_json(o);
}

static void reply_err(int id, const char *code, const char *msg)
{
    reply_err_at(id, code, msg, true);
}

static void send_event(const char *name, cJSON *data /* takes ownership */)
{
    cJSON *o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "event", name);
    cJSON_AddItemToObject(o, "data", data);
    ws_send_json(o);
}


/* ------------------------------------------------------------- Hilfsmittel */

static conn_t *conn_by_proxy(int h)
{
    for (int i = 0; i < MAX_CONN; i++)
        if (s_conn[i].in_use && s_conn[i].proxy == h) return &s_conn[i];
    return NULL;
}

static conn_t *conn_by_nimble(uint16_t h)
{
    for (int i = 0; i < MAX_CONN; i++)
        if (s_conn[i].in_use && s_conn[i].conn == h) return &s_conn[i];
    return NULL;
}

static conn_t *conn_alloc(void)
{
    for (int i = 0; i < MAX_CONN; i++)
        if (!s_conn[i].in_use) { memset(&s_conn[i], 0, sizeof(conn_t)); return &s_conn[i]; }
    return NULL;
}

/* The server may name a UUID in short form, canonical form or without dashes.
 * Parsing all three here means the rest of the code compares ble_uuid_t. */
static int parse_uuid(const char *txt, ble_uuid_any_t *out)
{
    if (!txt) return -1;
    size_t n = strlen(txt);
    char hex[33];
    size_t h = 0;
    for (size_t i = 0; i < n && h < 32; i++)
        if (txt[i] != '-') hex[h++] = txt[i];
    hex[h] = 0;

    if (h == 4) {
        out->u.type = BLE_UUID_TYPE_16;
        out->u16.value = (uint16_t)strtoul(hex, NULL, 16);
        return 0;
    }
    if (h == 32) {
        out->u.type = BLE_UUID_TYPE_128;
        for (int i = 0; i < 16; i++) {          /* NimBLE stores little-endian */
            char b[3] = { hex[i * 2], hex[i * 2 + 1], 0 };
            out->u128.value[15 - i] = (uint8_t)strtoul(b, NULL, 16);
        }
        return 0;
    }
    return -1;
}

static void uuid_to_text(const ble_uuid_t *u, char *out, size_t len)
{
    if (u->type == BLE_UUID_TYPE_16) {
        snprintf(out, len, "%04x", BLE_UUID16(u)->value);
    } else {
        ble_uuid_to_str(u, out);
    }
}

static int decode_b64(const char *txt, uint8_t **out, size_t *out_len)
{
    size_t n = strlen(txt), need = 0;
    mbedtls_base64_decode(NULL, 0, &need, (const unsigned char *)txt, n);
    uint8_t *buf = malloc(need ? need : 1);
    if (!buf) return -1;
    if (mbedtls_base64_decode(buf, need, out_len, (const unsigned char *)txt, n) != 0) {
        free(buf);
        return -1;
    }
    *out = buf;
    return 0;
}

/* Binary frame: opcode, connection handle big-endian, payload. */
static void send_binary(uint8_t opcode, uint16_t proxy_handle,
                        const uint8_t *payload, size_t len)
{
    uint8_t *frame = malloc(3 + len);
    if (!frame) return;
    frame[0] = opcode;
    frame[1] = (uint8_t)(proxy_handle >> 8);
    frame[2] = (uint8_t)(proxy_handle & 0xFF);
    if (len) memcpy(frame + 3, payload, len);
    msg_t m = { .kind = MSG_OUT_BIN, .data = frame, .len = 3 + len };
    q_put(&m);
}

static void op_clear(void)
{
    if (s_op.acc) { cJSON_Delete(s_op.acc); s_op.acc = NULL; }
    memset(&s_op, 0, sizeof(s_op));
}

static void op_fail(const char *code, const char *msg)
{
    int id = s_op.id;
    op_clear();
    reply_err(id, code, msg);
}

/* --------------------------------------------------------------- scannen */

static char *b64(const uint8_t *data, size_t len)
{
    size_t need = 0;
    mbedtls_base64_encode(NULL, 0, &need, data, len);
    char *out = malloc(need + 1);
    if (!out) return NULL;
    size_t wrote = 0;
    if (mbedtls_base64_encode((unsigned char *)out, need + 1, &wrote, data, len) != 0) {
        free(out);
        return NULL;
    }
    out[wrote] = 0;
    return out;
}

/* One advertisement -> one device_discovered event.  The field the server
 * actually needs is the service data under fff6: that is where a Matter device
 * puts its discriminator and vendor/product id. */
/* Even with duplicates allowed the same device can arrive ten times a second,
 * and every one of those costs a WebSocket frame over the backbone.  One
 * report per address per second is plenty for finding a device and leaves the
 * link to the host for what it is actually there for. */
#define REPORT_GAP_MS 1000
static struct { uint8_t addr[6]; bool with_data; int64_t last_ms; } s_seen[12];

/* The protocol names a peripheral by address only, but connecting also needs
 * its address type, and guessing it wrongly fails silently: the connect is
 * accepted and then simply never completes.  So remember what the scan saw. */
static struct { uint8_t addr[6]; uint8_t type; } s_addr_seen[12];
static int s_addr_seen_n;

static void remember_addr(const ble_addr_t *a)
{
    for (int i = 0; i < s_addr_seen_n; i++)
        if (memcmp(s_addr_seen[i].addr, a->val, 6) == 0) {
            s_addr_seen[i].type = a->type;
            return;
        }
    int i = s_addr_seen_n < 12 ? s_addr_seen_n++ : 0;
    memcpy(s_addr_seen[i].addr, a->val, 6);
    s_addr_seen[i].type = a->type;
}

static uint8_t addr_type_for(const uint8_t *val)
{
    for (int i = 0; i < s_addr_seen_n; i++)
        if (memcmp(s_addr_seen[i].addr, val, 6) == 0) return s_addr_seen[i].type;
    /* Not seen: the two top bits of a random static address are both set. */
    return ((val[5] & 0xC0) == 0xC0) ? BLE_ADDR_RANDOM : BLE_ADDR_PUBLIC;
}

/* Two classes per address, and the reason matters: an advertisement and its
 * scan response arrive as separate reports milliseconds apart, and the service
 * data — the part that says a device wants to be commissioned — often sits in
 * the scan response.  Limiting purely by address threw exactly that away and
 * made a device look like it was not there.
 */
static bool report_due(const uint8_t *addr, bool with_data)
{
    int64_t now = esp_timer_get_time() / 1000;
    int free_slot = -1;
    for (int i = 0; i < 12; i++) {
        if (memcmp(s_seen[i].addr, addr, 6) == 0 &&
            s_seen[i].with_data == with_data && s_seen[i].last_ms != 0) {
            if (now - s_seen[i].last_ms < REPORT_GAP_MS) return false;
            s_seen[i].last_ms = now;
            return true;
        }
        if (s_seen[i].last_ms == 0 && free_slot < 0) free_slot = i;
    }
    /* Nothing free: overwrite the oldest, the table is only a rate limiter. */
    int oldest = 0;
    for (int i = 1; i < 12; i++)
        if (s_seen[i].last_ms < s_seen[oldest].last_ms) oldest = i;
    int use = free_slot >= 0 ? free_slot : oldest;
    memcpy(s_seen[use].addr, addr, 6);
    s_seen[use].with_data = with_data;
    s_seen[use].last_ms = now;
    return true;
}

static void report_device(const struct ble_gap_disc_desc *d)
{
    struct ble_hs_adv_fields f;
    if (ble_hs_adv_parse_fields(&f, d->data, d->length_data) != 0) {
        return;
    }
    remember_addr(&d->addr);
    bool with_data = (f.svc_data_uuid16 != NULL && f.svc_data_uuid16_len > 2);
    if (!report_due(d->addr.val, with_data)) return;

    char addr[18];
    snprintf(addr, sizeof(addr), "%02x:%02x:%02x:%02x:%02x:%02x",
             d->addr.val[5], d->addr.val[4], d->addr.val[3],
             d->addr.val[2], d->addr.val[1], d->addr.val[0]);

    cJSON *o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "address", addr);
    cJSON_AddNumberToObject(o, "rssi", d->rssi);
    cJSON_AddBoolToObject(o, "connectable",
                          d->event_type == BLE_HCI_ADV_RPT_EVTYPE_ADV_IND ||
                          d->event_type == BLE_HCI_ADV_RPT_EVTYPE_DIR_IND);

    if (f.name != NULL && f.name_len > 0) {
        char nm[32];
        size_t n = f.name_len < sizeof(nm) - 1 ? f.name_len : sizeof(nm) - 1;
        memcpy(nm, f.name, n);
        nm[n] = 0;
        cJSON_AddStringToObject(o, "name", nm);
    }

    if (f.num_uuids16 > 0) {
        cJSON *arr = cJSON_CreateArray();
        for (int i = 0; i < f.num_uuids16; i++) {
            char u[5];
            snprintf(u, sizeof(u), "%04x", ble_uuid_u16(&f.uuids16[i].u));
            cJSON_AddItemToArray(arr, cJSON_CreateString(u));
        }
        cJSON_AddItemToObject(o, "service_uuids", arr);
    }

    if (with_data) {
        uint16_t uuid = f.svc_data_uuid16[0] | (f.svc_data_uuid16[1] << 8);
        char key[5];
        snprintf(key, sizeof(key), "%04x", uuid);
        char *enc = b64(f.svc_data_uuid16 + 2, f.svc_data_uuid16_len - 2);
        if (enc) {
            cJSON *sd = cJSON_CreateObject();
            cJSON_AddStringToObject(sd, key, enc);
            cJSON_AddItemToObject(o, "service_data", sd);
            free(enc);
        }
    }

    send_event("device_discovered", o);
}


/* ------------------------------------------------------------------- GATT */

static int on_disc_svc(uint16_t conn, const struct ble_gatt_error *err,
                       const struct ble_gatt_svc *svc, void *arg)
{
    (void)conn; (void)arg;
    if (err->status == 0 && svc) {
        char txt[BLE_UUID_STR_LEN];
        uuid_to_text(&svc->uuid.u, txt, sizeof(txt));
        cJSON *o = cJSON_CreateObject();
        cJSON_AddStringToObject(o, "uuid", txt);
        if (s_op.acc) cJSON_AddItemToArray(s_op.acc, o); else cJSON_Delete(o);
        /* Keep every service with its handle range.  Remembering only the
         * first was enough until the descriptors had to be found in advance —
         * then the range belonged to the wrong service and nothing was found. */
        if (s_op.c && s_op.c->n_svc < MAX_SVC) {
            int i = s_op.c->n_svc++;
            memcpy(&s_op.c->svc[i].uuid, &svc->uuid, sizeof(ble_uuid_any_t));
            s_op.c->svc[i].start = svc->start_handle;
            s_op.c->svc[i].end   = svc->end_handle;
        }
    } else if (err->status == BLE_HS_EDONE) {
        cJSON *res = cJSON_CreateObject();
        cJSON_AddItemToObject(res, "services", s_op.acc ? s_op.acc : cJSON_CreateArray());
        s_op.acc = NULL;
        int id = s_op.id;
        op_clear();
        reply_ok(id, res);
    } else {
        op_fail("discovery_failed", "service discovery failed");
    }
    return 0;
}

static int on_disc_dsc_map(uint16_t conn, const struct ble_gatt_error *err,
                           uint16_t chr_val_handle, const struct ble_gatt_dsc *dsc,
                           void *arg);

static int on_disc_chr(uint16_t conn, const struct ble_gatt_error *err,
                       const struct ble_gatt_chr *chr, void *arg)
{
    (void)conn; (void)arg;
    conn_t *c = s_op.c;
    if (err->status == 0 && chr && c) {
        char txt[BLE_UUID_STR_LEN];
        uuid_to_text(&chr->uuid.u, txt, sizeof(txt));

        if (c->n_chr < MAX_CHR) {
            memcpy(&c->chr[c->n_chr].uuid, &chr->uuid, sizeof(ble_uuid_any_t));
            c->chr[c->n_chr].val_handle = chr->val_handle;
            c->chr[c->n_chr].props      = chr->properties;
            c->n_chr++;
        }

        cJSON *o = cJSON_CreateObject();
        cJSON_AddStringToObject(o, "uuid", txt);
        cJSON *props = cJSON_CreateArray();
        if (chr->properties & BLE_GATT_CHR_PROP_READ)
            cJSON_AddItemToArray(props, cJSON_CreateString("read"));
        if (chr->properties & BLE_GATT_CHR_PROP_WRITE)
            cJSON_AddItemToArray(props, cJSON_CreateString("write"));
        if (chr->properties & BLE_GATT_CHR_PROP_WRITE_NO_RSP)
            cJSON_AddItemToArray(props, cJSON_CreateString("write-without-response"));
        if (chr->properties & BLE_GATT_CHR_PROP_NOTIFY)
            cJSON_AddItemToArray(props, cJSON_CreateString("notify"));
        if (chr->properties & BLE_GATT_CHR_PROP_INDICATE)
            cJSON_AddItemToArray(props, cJSON_CreateString("indicate"));
        cJSON_AddItemToObject(o, "properties", props);
        if (s_op.acc) cJSON_AddItemToArray(s_op.acc, o); else cJSON_Delete(o);
    } else if (err->status == BLE_HS_EDONE) {
        /* Find the client configuration descriptors now, while nothing is
         * waiting on the air.  Doing it later — between the BTP write and
         * enabling indications — costs round trips exactly when the peripheral
         * is about to answer, and the answer is then lost.  The protocol
         * specification warns about that race; this is how to lose it.
         */
        if (c && c->n_chr > 0) {
            s_op.kind = OP_DISC_DSC_MAP;
            if (ble_gattc_disc_all_dscs(c->conn,
                                        c->svc_start ? c->svc_start : 1,
                                        c->svc_end ? c->svc_end : 0xFFFF,
                                        on_disc_dsc_map, NULL) == 0) {
                return 0;                       /* reply once that is done */
            }
        }
        cJSON *res = cJSON_CreateObject();
        cJSON_AddItemToObject(res, "characteristics",
                              s_op.acc ? s_op.acc : cJSON_CreateArray());
        s_op.acc = NULL;
        int id = s_op.id;
        op_clear();
        reply_ok(id, res);
    } else {
        op_fail("discovery_failed", "characteristic discovery failed");
    }
    return 0;
}

static int on_disc_dsc_map(uint16_t conn, const struct ble_gatt_error *err,
                           uint16_t chr_val_handle, const struct ble_gatt_dsc *dsc,
                           void *arg)
{
    (void)conn; (void)arg;
    conn_t *c = s_op.c;
    if (err->status == 0 && dsc && c) {
        if (dsc->uuid.u.type == BLE_UUID_TYPE_16 &&
            BLE_UUID16(&dsc->uuid.u)->value == 0x2902) {
            /* Over a whole service range NimBLE reports the range's start as
             * chr_val_handle, not the characteristic the descriptor belongs
             * to — matching on it finds nothing.  A descriptor belongs to the
             * nearest characteristic below it, which is what the handle order
             * guarantees. */
            int best = -1;
            for (int i = 0; i < c->n_chr; i++)
                if (c->chr[i].val_handle < dsc->handle &&
                    (best < 0 || c->chr[i].val_handle > c->chr[best].val_handle))
                    best = i;
            if (best >= 0) {
                c->chr[best].cccd = dsc->handle;
                ESP_LOGD(TAG, "cccd %u belongs to characteristic at %u",
                         dsc->handle, c->chr[best].val_handle);
            }
        }
        return 0;
    }
    /* Done, or failed — either way answer with what was discovered.  A missing
     * descriptor is not fatal here; it only matters when someone subscribes. */
    cJSON *res = cJSON_CreateObject();
    cJSON_AddItemToObject(res, "characteristics",
                          s_op.acc ? s_op.acc : cJSON_CreateArray());
    s_op.acc = NULL;
    int id = s_op.id;
    op_clear();
    reply_ok(id, res);
    return 0;
}

static int on_read(uint16_t conn, const struct ble_gatt_error *err,
                   struct ble_gatt_attr *attr, void *arg)
{
    (void)conn; (void)arg;
    if (err->status != 0 || !attr) {
        op_fail("read_failed", "read failed");
        return 0;
    }
    uint16_t len = OS_MBUF_PKTLEN(attr->om);
    uint8_t *buf = malloc(len ? len : 1);
    if (buf) ble_hs_mbuf_to_flat(attr->om, buf, len, NULL);

    if (!buf) {
        op_fail("read_failed", "out of memory");
        return 0;
    }
    if (s_op.binary_read && s_op.c) {
        send_binary(0x03, s_op.c->proxy, buf, len);
        int id = s_op.id;
        op_clear();
        reply_ok(id, NULL);
    } else {
        char *enc = b64(buf, len);
        cJSON *res = cJSON_CreateObject();
        cJSON_AddStringToObject(res, "value", enc ? enc : "");
        free(enc);
        int id = s_op.id;
        op_clear();
        reply_ok(id, res);
    }
    free(buf);
    return 0;
}

int on_write(uint16_t conn, const struct ble_gatt_error *err,
             struct ble_gatt_attr *attr, void *arg)
{
    (void)conn; (void)attr; (void)arg;
    if (err->status != 0) {
        op_fail(s_op.kind == OP_SUB_WRITE_CCCD ? "subscribe_failed" : "write_failed",
                "write failed");
        return 0;
    }

    /* write_and_subscribe: the write is acknowledged, enable the subscription
     * immediately.  The descriptor was located during discovery precisely so
     * that nothing happens on the air between these two steps. */
    if (s_op.kind == OP_WRITE && s_op.subscribe_after) {
        conn_t *c = s_op.c;
        int idx = -1;
        for (int i = 0; i < c->n_chr; i++)
            if (ble_uuid_cmp(&c->chr[i].uuid.u, &s_op.sub_uuid.u) == 0) { idx = i; break; }
        if (idx < 0) {
            op_fail("characteristic_not_found", "subscribe characteristic unknown");
            return 0;
        }
        if (c->chr[idx].cccd == 0) {
            op_fail("subscribe_failed", "no client configuration descriptor");
            return 0;
        }
        uint8_t val[2] = { 0x01, 0x00 };
        if (c->chr[idx].props & BLE_GATT_CHR_PROP_INDICATE) val[0] = 0x02;
        c->notify_target = c->chr[idx].val_handle;
        s_op.kind = OP_SUB_WRITE_CCCD;
        ESP_LOGI(TAG, "enabling %s on handle %u",
                 val[0] == 0x02 ? "indications" : "notifications", c->chr[idx].cccd);
        if (ble_gattc_write_flat(c->conn, c->chr[idx].cccd, val, sizeof(val),
                                 on_write, NULL) != 0) {
            op_fail("subscribe_failed", "could not write the descriptor");
        }
        return 0;
    }

    int id = s_op.id;
    op_clear();
    reply_ok(id, NULL);
    return 0;
}

static int on_mtu(uint16_t conn, const struct ble_gatt_error *err,
                  uint16_t mtu, void *arg)
{
    (void)conn; (void)arg;
    bool for_connect = (s_op.kind == OP_CONNECT_MTU);

    if (err->status != 0) {
        /* A refused exchange is not a refused connection: the link stays
         * usable, only in small pieces.  Answer the connect with what the
         * link already has rather than failing it. */
        if (!for_connect) {
            op_fail("mtu_request_failed", "MTU exchange failed");
            return 0;
        }
        ESP_LOGW(TAG, "MTU exchange failed (%d), staying at %u",
                 err->status, s_op.c ? s_op.c->mtu : 0);
        mtu = s_op.c ? s_op.c->mtu : 23;
    } else if (s_op.c) {
        s_op.c->mtu = mtu;
    }

    cJSON *res = cJSON_CreateObject();
    if (for_connect) {
        ESP_LOGI(TAG, "ATT MTU %u", mtu);
        cJSON_AddNumberToObject(res, "connection_handle",
                                s_op.c ? s_op.c->proxy : 0);
    }
    cJSON_AddNumberToObject(res, "mtu", mtu);
    int id = s_op.id;
    op_clear();
    reply_ok(id, res);
    return 0;
}

static int gap_event(struct ble_gap_event *event, void *arg)
{
    (void)arg;
    switch (event->type) {
    case BLE_GAP_EVENT_DISC:
        report_device(&event->disc);
        break;

    case BLE_GAP_EVENT_DISC_COMPLETE:
        s_scanning = false;
        send_event("scan_stopped", cJSON_CreateObject());
        break;

    case BLE_GAP_EVENT_CONNECT:
        if (s_op.kind != OP_CONNECT || !s_op.c) break;
        if (event->connect.status != 0) {
            ESP_LOGW(TAG, "connect failed, status %d", event->connect.status);
            s_op.c->in_use = false;
            op_fail("connection_failed", "connect failed");
            break;
        }
        ESP_LOGI(TAG, "connected, handle %u", event->connect.conn_handle);
        s_op.c->conn = event->connect.conn_handle;
        s_op.c->mtu  = ble_att_mtu(event->connect.conn_handle);
        /* Ask for a bigger ATT_MTU before answering.  The transport slices its
         * packets to whatever this reply reports, once, for the whole session:
         * answering straight away reports the 23 bytes a fresh link starts
         * with, and every certificate then crosses the air in 20-byte pieces.
         * The peripheral asks for the exchange itself right after connecting —
         * that request is dropped here, there being no ATT server on a
         * central-only build — so the central has to ask. */
        s_op.kind = OP_CONNECT_MTU;
        if (ble_gattc_exchange_mtu(s_op.c->conn, on_mtu, NULL) != 0) {
            cJSON *res = cJSON_CreateObject();
            cJSON_AddNumberToObject(res, "connection_handle", s_op.c->proxy);
            cJSON_AddNumberToObject(res, "mtu", s_op.c->mtu);
            int id = s_op.id;
            op_clear();
            reply_ok(id, res);
        }
        break;

    case BLE_GAP_EVENT_DISCONNECT: {
        conn_t *c = conn_by_nimble(event->disconnect.conn.conn_handle);
        if (c) {
            cJSON *d = cJSON_CreateObject();
            cJSON_AddNumberToObject(d, "connection_handle", c->proxy);
            cJSON_AddStringToObject(d, "reason", "peripheral disconnected");
            send_event("disconnected", d);
            c->in_use = false;
        }
        break;
    }

    case BLE_GAP_EVENT_NOTIFY_RX: {
        conn_t *c = conn_by_nimble(event->notify_rx.conn_handle);
        if (!c) break;
        uint16_t len = OS_MBUF_PKTLEN(event->notify_rx.om);
        uint8_t *buf = malloc(len ? len : 1);
        if (buf) {
            ble_hs_mbuf_to_flat(event->notify_rx.om, buf, len, NULL);
            /* Binary: during commissioning these arrive back to back, and a
             * JSON envelope per packet would cost more than it carries. */
            send_binary(0x02, c->proxy, buf, len);
            free(buf);
        }
        break;
    }

    case BLE_GAP_EVENT_MTU: {
        conn_t *c = conn_by_nimble(event->mtu.conn_handle);
        if (c) c->mtu = event->mtu.value;
        break;
    }

    default:
        break;
    }
    return 0;
}

/* Scanning and Thread share one radio.  Left at the stack's defaults the scan
 * receives almost continuously, and the border router then cannot get its own
 * packets out — measured: a commissioning scan filled the log with
 * ChannelAccessFailure while Thread traffic stalled.  A duty cycle of roughly
 * 30 % leaves the mesh its air time and still finds a device advertising every
 * few hundred milliseconds.
 *
 * Units are 0.625 ms: 160 = 100 ms interval, 48 = 30 ms window.
 */
#define SCAN_ITVL    160
#define SCAN_WINDOW   48

static int start_scan(bool allow_duplicates)
{
    struct ble_gap_disc_params p = {
        .itvl = SCAN_ITVL,
        .window = SCAN_WINDOW,
        .filter_policy = 0,
        .limited = 0,
        .passive = 0,              /* active: ask for the scan response too */
        .filter_duplicates = allow_duplicates ? 0 : 1,
    };
    return ble_gap_disc(s_own_addr_type, BLE_HS_FOREVER, &p, gap_event, NULL);
}

/* ------------------------------------------------------------- Befehle */

static void handle_command(cJSON *msg)
{
    const cJSON *jid  = cJSON_GetObjectItem(msg, "id");
    const cJSON *jcmd = cJSON_GetObjectItem(msg, "command");
    if (!cJSON_IsNumber(jid) || !cJSON_IsString(jcmd)) {
        return;
    }
    int id = jid->valueint;
    const char *cmd = jcmd->valuestring;
    ESP_LOGI(TAG, "command %s (id %d)", cmd, id);

    if (strcmp(cmd, "start_scan") == 0) {
        if (s_scanning) {
            reply_err(id, "already_scanning", "a scan is already running");
            return;
        }
        const cJSON *jargs = cJSON_GetObjectItem(msg, "args");
        const cJSON *jdup = jargs ? cJSON_GetObjectItem(jargs, "allow_duplicates") : NULL;
        bool dup = jdup ? cJSON_IsTrue(jdup) : true;   /* spec default */
        memset(s_seen, 0, sizeof(s_seen));
        int rc = start_scan(dup);
        if (rc != 0) {
            reply_err(id, "bluetooth_unavailable", "ble_gap_disc failed");
            return;
        }
        s_scanning = true;
        reply_ok(id, NULL);
        return;
    }

    if (strcmp(cmd, "stop_scan") == 0) {
        if (!s_scanning) {
            reply_err_at(id, "not_scanning", "no scan is running", false);
            return;
        }
        ble_gap_disc_cancel();
        s_scanning = false;
        reply_ok(id, NULL);
        send_event("scan_stopped", cJSON_CreateObject());
        return;
    }

    /* One operation at a time.  Every GATT command below parks its state in a
     * single slot that the NimBLE callback picks up again; a second command
     * arriving meanwhile would overwrite it, and the callback still in flight
     * would then answer about the wrong thing.  The server does serialise, but
     * it also runs several commissioning candidates in parallel — so say no
     * rather than corrupt. */
    if (s_op.kind != OP_NONE) {
        ESP_LOGW(TAG, "%s rejected: an operation is still in flight", cmd);
        reply_err(id, "busy", "another operation is in flight");
        return;
    }

    /* From here on every command names a connection. */
    const cJSON *args = cJSON_GetObjectItem(msg, "args");
    const cJSON *jh   = args ? cJSON_GetObjectItem(args, "connection_handle") : NULL;

    if (strcmp(cmd, "connect") == 0) {
        const cJSON *jaddr = args ? cJSON_GetObjectItem(args, "address") : NULL;
        if (!cJSON_IsString(jaddr)) { reply_err(id, "connection_failed", "no address"); return; }
        unsigned b[6];
        if (sscanf(jaddr->valuestring, "%x:%x:%x:%x:%x:%x",
                   &b[0], &b[1], &b[2], &b[3], &b[4], &b[5]) != 6) {
            reply_err(id, "connection_failed", "malformed address");
            return;
        }
        conn_t *c = conn_alloc();
        if (!c) { reply_err(id, "connection_failed", "no free connection slot"); return; }
        ble_addr_t peer = { 0 };
        for (int i = 0; i < 6; i++) peer.val[i] = (uint8_t)b[5 - i];
        peer.type = addr_type_for(peer.val);
        memcpy(c->addr, peer.val, 6);
        c->in_use = true;
        c->proxy  = s_next_proxy_handle++;

        if (s_scanning) { ble_gap_disc_cancel(); s_scanning = false; }

        op_clear();
        s_op.kind = OP_CONNECT;
        s_op.id   = id;
        s_op.c    = c;
        ESP_LOGI(TAG, "connecting to %s (%s address)", jaddr->valuestring,
                 peer.type == BLE_ADDR_PUBLIC ? "public" : "random");
        int rc = ble_gap_connect(s_own_addr_type, &peer, 30000, NULL, gap_event, NULL);
        if (rc != 0) { c->in_use = false; op_fail("device_not_found", "connect could not start"); }
        return;
    }

    conn_t *c = cJSON_IsNumber(jh) ? conn_by_proxy(jh->valueint) : NULL;
    if (!c) { reply_err(id, "not_connected", "unknown connection handle"); return; }

    if (strcmp(cmd, "disconnect") == 0) {
        int rc = ble_gap_terminate(c->conn, BLE_ERR_REM_USER_CONN_TERM);
        if (rc != 0 && rc != BLE_HS_ENOTCONN) {
            reply_err(id, "not_connected", "could not terminate the link");
            return;
        }
        /* The slot is released when the disconnect event arrives, not here:
         * letting it go early would hand the same handle to a new connection
         * while the old link is still coming down. */
        if (rc == BLE_HS_ENOTCONN) c->in_use = false;
        reply_ok(id, NULL);
        return;
    }

    if (strcmp(cmd, "discover_services") == 0) {
        op_clear();
        s_op.kind = OP_DISC_SVC; s_op.id = id; s_op.c = c;
        s_op.acc = cJSON_CreateArray();
        c->n_chr = 0; c->n_svc = 0;
        if (ble_gattc_disc_all_svcs(c->conn, on_disc_svc, NULL) != 0)
            op_fail("discovery_failed", "could not start service discovery");
        return;
    }

    if (strcmp(cmd, "discover_characteristics") == 0) {
        op_clear();
        s_op.kind = OP_DISC_CHR; s_op.id = id; s_op.c = c;
        s_op.acc = cJSON_CreateArray();
        c->n_chr = 0;
        uint16_t from = 1, to = 0xFFFF;
        const cJSON *jsvc = args ? cJSON_GetObjectItem(args, "service_uuid") : NULL;
        ble_uuid_any_t want_svc;
        if (cJSON_IsString(jsvc) && parse_uuid(jsvc->valuestring, &want_svc) == 0) {
            for (int i = 0; i < c->n_svc; i++)
                if (ble_uuid_cmp(&c->svc[i].uuid.u, &want_svc.u) == 0) {
                    from = c->svc[i].start; to = c->svc[i].end; break;
                }
        }
        c->svc_start = from; c->svc_end = to;
        if (ble_gattc_disc_all_chrs(c->conn, from, to, on_disc_chr, NULL) != 0)
            op_fail("discovery_failed", "could not start characteristic discovery");
        return;
    }

    /* The remaining commands all name a characteristic by UUID. */
    const char *uuid_key = strcmp(cmd, "write_and_subscribe") == 0 ? "write_uuid"
                                                                  : "characteristic_uuid";
    const cJSON *ju = args ? cJSON_GetObjectItem(args, uuid_key) : NULL;
    ble_uuid_any_t want;
    int idx = -1;
    if (cJSON_IsString(ju) && parse_uuid(ju->valuestring, &want) == 0) {
        for (int i = 0; i < c->n_chr; i++)
            if (ble_uuid_cmp(&c->chr[i].uuid.u, &want.u) == 0) { idx = i; break; }
    }

    if (strcmp(cmd, "request_mtu") == 0) {
        op_clear();
        s_op.kind = OP_MTU; s_op.id = id; s_op.c = c;
        if (ble_gattc_exchange_mtu(c->conn, on_mtu, NULL) != 0)
            op_fail("mtu_request_failed", "MTU exchange could not start");
        return;
    }

    if (idx < 0) { reply_err(id, "characteristic_not_found", "unknown characteristic"); return; }

    if (strcmp(cmd, "read_characteristic") == 0) {
        op_clear();
        s_op.kind = OP_READ; s_op.id = id; s_op.c = c;
        if (ble_gattc_read(c->conn, c->chr[idx].val_handle, on_read, NULL) != 0)
            op_fail("read_failed", "read could not start");
        return;
    }

    if (strcmp(cmd, "write_characteristic") == 0 || strcmp(cmd, "write_and_subscribe") == 0) {
        const char *vkey = strcmp(cmd, "write_and_subscribe") == 0 ? "write_value" : "value";
        const cJSON *jv = args ? cJSON_GetObjectItem(args, vkey) : NULL;
        if (!cJSON_IsString(jv)) { reply_err(id, "write_failed", "no value"); return; }
        uint8_t *data = NULL; size_t dlen = 0;
        if (decode_b64(jv->valuestring, &data, &dlen) != 0) {
            reply_err(id, "write_failed", "value is not base64");
            return;
        }
        const cJSON *jr = args ? cJSON_GetObjectItem(args,
                              strcmp(cmd, "write_and_subscribe") == 0 ? "write_response"
                                                                     : "response") : NULL;
        bool with_response = cJSON_IsTrue(jr);

        c->write_target = c->chr[idx].val_handle;
        op_clear();
        s_op.kind = OP_WRITE; s_op.id = id; s_op.c = c;
        s_op.target = c->chr[idx].val_handle;
        if (strcmp(cmd, "write_and_subscribe") == 0) {
            const cJSON *js = cJSON_GetObjectItem(args, "subscribe_uuid");
            if (!cJSON_IsString(js) || parse_uuid(js->valuestring, &s_op.sub_uuid) != 0) {
                free(data);
                op_fail("characteristic_not_found", "bad subscribe uuid");
                return;
            }
            s_op.subscribe_after = true;
        }
        int rc = with_response || s_op.subscribe_after
               ? ble_gattc_write_flat(c->conn, c->chr[idx].val_handle, data, dlen, on_write, NULL)
               : ble_gattc_write_no_rsp_flat(c->conn, c->chr[idx].val_handle, data, dlen);
        free(data);
        if (rc != 0) { op_fail("write_failed", "write could not start"); return; }
        if (!with_response && !s_op.subscribe_after) {     /* no callback comes */
            int i2 = s_op.id; op_clear(); reply_ok(i2, NULL);
        }
        return;
    }

    if (strcmp(cmd, "subscribe_characteristic") == 0) {
        c->notify_target = c->chr[idx].val_handle;
        op_clear();
        if (c->chr[idx].cccd == 0) {
            reply_err(id, "subscribe_failed", "no client configuration descriptor");
            return;
        }
        uint8_t val[2] = { 0x01, 0x00 };
        if (c->chr[idx].props & BLE_GATT_CHR_PROP_INDICATE) val[0] = 0x02;
        op_clear();
        s_op.kind = OP_SUB_WRITE_CCCD; s_op.id = id; s_op.c = c;
        if (ble_gattc_write_flat(c->conn, c->chr[idx].cccd, val, sizeof(val),
                                 on_write, NULL) != 0)
            op_fail("subscribe_failed", "could not write the descriptor");
        return;
    }

    if (strcmp(cmd, "unsubscribe_characteristic") == 0) {
        if (c->chr[idx].cccd == 0) { reply_ok(id, NULL); return; }
        uint8_t off[2] = { 0x00, 0x00 };
        op_clear();
        s_op.kind = OP_SUB_WRITE_CCCD; s_op.id = id; s_op.c = c;
        if (ble_gattc_write_flat(c->conn, c->chr[idx].cccd, off, sizeof(off),
                                 on_write, NULL) != 0)
            op_fail("subscribe_failed", "could not write the descriptor");
        return;
    }

    reply_err(id, "not_supported", "unknown command");
}

/* Binary in: opcode 0x01 writes BTP data to whichever characteristic the last
 * write_characteristic named for this connection — the frame carries no UUID,
 * by design, because during commissioning it is always the same one. */
static void handle_binary(const uint8_t *frame, int len)
{
    uint8_t op = frame[0];
    uint16_t handle = (uint16_t)((frame[1] << 8) | frame[2]);
    const uint8_t *payload = frame + 3;
    int plen = len - 3;

    if (op != 0x01) {
        ESP_LOGW(TAG, "unexpected binary opcode 0x%02x", op);
        return;
    }
    conn_t *c = conn_by_proxy(handle);
    if (!c || c->write_target == 0) {
        ESP_LOGW(TAG, "binary write for unknown connection %u", handle);
        return;
    }
    /* The spec requires an acknowledged write for BTP data on C1. */
    int rc = ble_gattc_write_flat(c->conn, c->write_target, payload, plen, NULL, NULL);
    if (rc != 0) {
        ESP_LOGW(TAG, "binary write failed: %d", rc);
    }
}

static void handle_text(const char *data, int len)
{
    cJSON *msg = cJSON_ParseWithLength(data, len);
    if (!msg) {
        ESP_LOGW(TAG, "unparseable message (%d bytes)", len);
        return;
    }
    const cJSON *type = cJSON_GetObjectItem(msg, "type");
    if (cJSON_IsString(type) && strcmp(type->valuestring, "hello_response") == 0) {
        const cJSON *err = cJSON_GetObjectItem(msg, "error");
        if (cJSON_IsString(err)) {
            ESP_LOGE(TAG, "handshake refused: %s", err->valuestring);
        } else {
            ESP_LOGI(TAG, "handshake accepted, protocol version %d",
                     cJSON_GetObjectItem(msg, "version")
                         ? cJSON_GetObjectItem(msg, "version")->valueint : -1);
        }
        cJSON_Delete(msg);
        return;
    }
    handle_command(msg);
    cJSON_Delete(msg);
}

/* ------------------------------------------------------------- WebSocket */

static void ws_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg; (void)base;
    esp_websocket_event_data_t *e = (esp_websocket_event_data_t *)data;

    switch (id) {
    case WEBSOCKET_EVENT_CONNECTED: {
        s_was_connected = true;
        s_quiet_since = 0;
        ESP_LOGI(TAG, "connected to the Matter server, saying hello");
        cJSON *o = cJSON_CreateObject();
        cJSON_AddStringToObject(o, "type", "hello");
        cJSON_AddNumberToObject(o, "version", PROTOCOL_VERSION);
        ws_send_json(o);                 /* queued, sent by the proxy task */
        break;
    }
    case WEBSOCKET_EVENT_DATA: {
        /* Copy and hand over.  Nothing is parsed and nothing is answered here:
         * this runs in the client's own event handler, where sending is not
         * allowed. */
        if (e->data_len <= 0) break;
        msg_kind_t kind;
        if (e->op_code == 0x01)      kind = MSG_IN_TEXT;
        else if (e->op_code == 0x02) kind = MSG_IN_BIN;
        else break;
        if (kind == MSG_IN_BIN && e->data_len < 3) break;
        uint8_t *copy = malloc(e->data_len);
        if (!copy) break;
        memcpy(copy, e->data_ptr, e->data_len);
        msg_t m = { .kind = kind, .data = copy, .len = e->data_len };
        q_put(&m);
        break;
    }
    case WEBSOCKET_EVENT_DISCONNECTED:
        /* Say it once, not every five seconds.  A stick on a machine with no
         * Matter server retries forever, and each attempt used to put six
         * lines in the log — enough to push everything else out of it. */
        if (s_was_connected) {
            ESP_LOGW(TAG, "lost the link to the Matter server at %s — retrying",
                     s_uri[0] ? s_uri : "the configured address");
            s_quiet_since = esp_timer_get_time();
        } else if (s_quiet_since == 0 ||
                   esp_timer_get_time() - s_quiet_since > 60 * 1000000LL) {
            ESP_LOGW(TAG, "no Matter server at %s — retrying",
                     s_uri[0] ? s_uri : "the configured address");
            s_quiet_since = esp_timer_get_time();
        }
        s_was_connected = false;
        if (s_scanning) {
            ble_gap_disc_cancel();
            s_scanning = false;
        }
        break;
    default:
        break;
    }
}

/* ------------------------------------------------------------- NimBLE */

static void on_sync(void)
{
    ble_hs_util_ensure_addr(0);
    if (ble_hs_id_infer_auto(0, &s_own_addr_type) != 0) {
        ESP_LOGE(TAG, "no usable BLE address");
        return;
    }
    ESP_LOGI(TAG, "BLE ready, free heap %" PRIu32, esp_get_free_heap_size());
}

static void host_task(void *param)
{
    (void)param;
    nimble_port_run();
    nimble_port_freertos_deinit();
}

/* The only place that touches the socket for sending, and the only place that
 * runs BLE operations on behalf of a command. */
static void proxy_task(void *arg)
{
    (void)arg;
    msg_t m;
    for (;;) {
        if (xQueueReceive(s_q, &m, portMAX_DELAY) != pdTRUE) continue;
        switch (m.kind) {
        case MSG_IN_TEXT:
            handle_text((const char *)m.data, (int)m.len);
            break;
        case MSG_IN_BIN:
            handle_binary(m.data, (int)m.len);
            break;
        case MSG_OUT_TEXT:
            if (s_ws && esp_websocket_client_is_connected(s_ws)) {
                /* A finite timeout: a socket that cannot take the message in
                 * two seconds is a socket in trouble, and waiting forever on
                 * it is how this task would stop being the solution. */
                esp_websocket_client_send_text(s_ws, (const char *)m.data,
                                               (int)m.len, pdMS_TO_TICKS(2000));
            }
            break;
        case MSG_OUT_BIN:
            if (s_ws && esp_websocket_client_is_connected(s_ws)) {
                esp_websocket_client_send_bin(s_ws, (const char *)m.data,
                                              (int)m.len, pdMS_TO_TICKS(2000));
            }
            break;
        }
        free(m.data);
    }
}

void thbr_ble_proxy_start(const char *uri)
{
    uint32_t before = esp_get_free_heap_size();

    if (nimble_port_init() != ESP_OK) {
        ESP_LOGE(TAG, "nimble_port_init failed");
        return;
    }
    ble_hs_cfg.sync_cb = on_sync;
    nimble_port_freertos_init(host_task);

    /* A websocket that dies quietly is worse than one that fails loudly.  It
     * happened here: the connection went half-open, no close arrived, nothing
     * reconnected, and the server went on sending commands into a socket that
     * had nobody at the other end — every one of them timing out.  So: ping on
     * a schedule, give up on the connection when the pongs stop, and let TCP
     * keep-alive catch the case where even the ping cannot leave. */
    esp_websocket_client_config_t cfg = {
        .uri                  = uri,
        .reconnect_timeout_ms = 5000,
        .network_timeout_ms   = 10000,
        .ping_interval_sec    = 10,
        .pingpong_timeout_sec = 25,
        .keep_alive_enable    = true,
        .keep_alive_idle      = 5,
        .keep_alive_interval   = 5,
        .keep_alive_count     = 3,
    };
    /* The transport layers below say the same thing four more times, at error
     * level, on every attempt.  One line from here is the whole story. */
    esp_log_level_set("websocket_client", ESP_LOG_NONE);
    esp_log_level_set("transport_ws",     ESP_LOG_NONE);
    esp_log_level_set("transport_base",   ESP_LOG_NONE);
    esp_log_level_set("esp-tls",          ESP_LOG_NONE);
    snprintf(s_uri, sizeof(s_uri), "%s", uri ? uri : "");

    s_ws = esp_websocket_client_init(&cfg);
    if (!s_ws) {
        ESP_LOGE(TAG, "websocket client init failed");
        return;
    }
    s_q = xQueueCreate(Q_DEPTH, sizeof(msg_t));
    if (!s_q) {
        ESP_LOGE(TAG, "could not create the message queue");
        return;
    }
    if (xTaskCreate(proxy_task, "ble_proxy", 5120, NULL, 5, NULL) != pdPASS) {
        ESP_LOGE(TAG, "could not start the proxy task");
        return;
    }

    esp_websocket_register_events(s_ws, WEBSOCKET_EVENT_ANY, ws_event, NULL);
    esp_websocket_client_start(s_ws);

    ESP_LOGI(TAG, "BLE proxy started for %s; heap %" PRIu32 " -> %" PRIu32,
             uri, before, esp_get_free_heap_size());
}

#else
void thbr_ble_proxy_start(const char *uri) { (void)uri; }
#endif
