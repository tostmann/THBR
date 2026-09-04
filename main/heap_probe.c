/* Heap census — instrumentation for the heap drift on a loaded border router.
 *
 * WHY THIS EXISTS
 *
 * A production border router loses free heap steadily under load while a bench
 * stick carrying the same firmware and heavier synthetic load stays flat.  Every
 * candidate that the stick LOGS was ruled out by counting it on both sides: the
 * bench has more of each and does not drift.  So the difference is not in the
 * log, and `esp_get_free_heap_size()` alone cannot say what is going: a falling
 * number is equally consistent with a leak, with fragmentation, and with a
 * cache that legitimately grew.
 *
 * These three tell those apart:
 *
 *   total_allocated_bytes  rises on a leak, flat on fragmentation
 *   allocated_blocks       with the above gives the MEAN SIZE of what leaks,
 *                          which is the strongest hint at the culprit
 *   largest_free_block     falls faster than total_free only on fragmentation
 *
 * WHAT THE FIRST CENSUS TAUGHT (2026-09-04, firmware 0.1.52)
 *
 * The idle level of allocated bytes on the loaded router stays constant for
 * hours and then steps: single blocks of ~1.2 KB and a few dozen of ~130 B per
 * day, each step in the hour after four Thread nodes that share a switched
 * circuit registered with the SRP server and then went dark until their lease
 * ran out.  The bench never steps.  A total says that much and no more, so
 * this build adds the two views that name the owner:
 *
 *   per task      every block carries the task that allocated it
 *                 (CONFIG_HEAP_TASK_TRACKING); the totals split into ot_main,
 *                 tiT (lwIP), mdns, httpd, ... and the one that grows is the
 *                 suspect.  Sampled unattended with the census.
 *   per caller    the LEAKS-mode trace, now with a real call chain: the first
 *                 build had CONFIG_HEAP_TRACING_STACK_DEPTH clamped to 0 on
 *                 this RISC-V target (no frame pointer), so its "callers" were
 *                 list-pointer garbage.  With the frame pointer on, the chain
 *                 is real up to the prebuilt border-router library, whose
 *                 entry address the ELF still names.
 *
 * The trace costs a buffer out of the very heap under investigation, so it is
 * off until asked for; the census and the per-task split are what run
 * unattended.
 *
 * Compiled only under CONFIG_THBR_HEAP_PROBE.
 */

#include "heap_probe.h"

#if CONFIG_THBR_HEAP_PROBE

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "cJSON.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#if CONFIG_HEAP_TRACING_STANDALONE
#include "esp_heap_trace.h"
#endif
#if CONFIG_HEAP_TASK_TRACKING
#include "esp_heap_task_info.h"
#endif

static const char *TAG = "thbr_heap";

#if CONFIG_HEAP_TRACING_STANDALONE
static bool s_on;               /* trace running */
#endif

/* ------------------------------------------------------------------ census */

static void region_json(char *out, size_t len, uint32_t caps)
{
    multi_heap_info_t i;
    heap_caps_get_info(&i, caps);
    snprintf(out, len,
             "{\"free\":%u,\"alloc\":%u,\"largest\":%u,\"min\":%u,"
             "\"ablocks\":%u,\"fblocks\":%u,\"tblocks\":%u}",
             (unsigned)i.total_free_bytes, (unsigned)i.total_allocated_bytes,
             (unsigned)i.largest_free_block, (unsigned)i.minimum_free_bytes,
             (unsigned)i.allocated_blocks, (unsigned)i.free_blocks,
             (unsigned)i.total_blocks);
}

#if CONFIG_HEAP_TASK_TRACKING
/* One line per task: bytes and blocks it currently holds.  A task that has
 * been deleted still owns blocks under its old handle; those show up under
 * the handle in hex so they are counted rather than lost. */
#define MAX_TASK_TOTALS 24

static esp_err_t send_per_task(httpd_req_t *req)
{
    static heap_task_totals_t totals[MAX_TASK_TOTALS];
    size_t num_totals = 0;
    heap_task_info_params_t p = {
        .caps = {0}, .mask = {0},            /* no partitioning: one total per task */
        .tasks = NULL, .num_tasks = 0,
        .totals = totals, .num_totals = &num_totals, .max_totals = MAX_TASK_TOTALS,
        .blocks = NULL, .max_blocks = 0,
    };
    heap_caps_get_per_task_info(&p);

#if CONFIG_FREERTOS_USE_TRACE_FACILITY
    /* The handle -> name table for tasks that still exist. */
    TaskStatus_t status[MAX_TASK_TOTALS];
    UBaseType_t ntasks = uxTaskGetSystemState(status, MAX_TASK_TOTALS, NULL);
#endif

    esp_err_t err = httpd_resp_sendstr_chunk(req, ",\"per_task\":[");
    for (size_t i = 0; i < num_totals && err == ESP_OK; i++) {
        char name[24];
        snprintf(name, sizeof(name), "0x%08" PRIx32, (uint32_t)(uintptr_t)totals[i].task);
        if (totals[i].task == NULL) {
            snprintf(name, sizeof(name), "pre-scheduler");
        }
#if CONFIG_FREERTOS_USE_TRACE_FACILITY
        for (UBaseType_t t = 0; t < ntasks; t++) {
            if (status[t].xHandle == totals[i].task) {
                snprintf(name, sizeof(name), "%s", status[t].pcTaskName);
                break;
            }
        }
#endif
        char one[96];
        snprintf(one, sizeof(one), "%s{\"task\":\"%s\",\"alloc\":%u,\"blocks\":%u}",
                 i ? "," : "", name, (unsigned)totals[i].size[0], (unsigned)totals[i].count[0]);
        err = httpd_resp_sendstr_chunk(req, one);
    }
    if (err == ESP_OK) {
        err = httpd_resp_sendstr_chunk(req, "]");
    }
    return err;
}
#endif /* CONFIG_HEAP_TASK_TRACKING */

static esp_err_t heap_get(httpd_req_t *req)
{
    char g8[192], dma[192];
    region_json(g8, sizeof(g8), MALLOC_CAP_8BIT);
    region_json(dma, sizeof(dma), MALLOC_CAP_DMA);

    const char *trace = "{\"on\":false}";
#if CONFIG_HEAP_TRACING_STANDALONE
    char tbuf[224];
    heap_trace_summary_t s;
    if (s_on && heap_trace_summary(&s) == ESP_OK) {
        /* allocations minus frees is the net number of blocks the trace window
         * is still holding — the leak, counted, before anyone reads a stack. */
        snprintf(tbuf, sizeof(tbuf),
                 "{\"on\":true,\"records\":%u,\"capacity\":%u,\"high\":%u,\"overflow\":%s,"
                 "\"allocs\":%u,\"frees\":%u}",
                 (unsigned)s.count, (unsigned)s.capacity, (unsigned)s.high_water_mark,
                 s.has_overflowed ? "true" : "false",
                 (unsigned)s.total_allocations, (unsigned)s.total_frees);
        trace = tbuf;
    }
#endif

    /* Sized for the compiler's worst case, not the real one: it has to assume
     * every %s fills its source buffer, which sums to ~716 bytes.  The actual
     * answer is around 300.  -Werror=format-truncation is on, so the buffer
     * has to satisfy the pessimistic sum. */
    char body[1024];
    snprintf(body, sizeof(body),
             "{\"uptime_s\":%" PRId64 ",\"free\":%" PRIu32 ",\"min_free\":%" PRIu32
             ",\"tasks\":%u,\"cap8\":%s,\"dma\":%s,\"trace\":%s",
             esp_timer_get_time() / 1000000,
             esp_get_free_heap_size(), esp_get_minimum_free_heap_size(),
             (unsigned)uxTaskGetNumberOfTasks(), g8, dma, trace);

    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    /* Chunked, because the per-task list has no fixed size and the body above
     * is already close to what a stack frame should carry. */
    esp_err_t err = httpd_resp_sendstr_chunk(req, body);
#if CONFIG_HEAP_TASK_TRACKING
    if (err == ESP_OK) {
        err = send_per_task(req);
    }
#endif
    if (err == ESP_OK) {
        err = httpd_resp_sendstr_chunk(req, "}");
    }
    if (err == ESP_OK) {
        httpd_resp_sendstr_chunk(req, NULL);
    }
    return err;
}

/* ------------------------------------------------------------------- trace */

#if CONFIG_HEAP_TRACING_STANDALONE

static heap_trace_record_t *s_buf;
static size_t               s_cap;

static esp_err_t trace_start(size_t records, char *err, size_t errlen)
{
    if (s_on) {
        snprintf(err, errlen, "already running with %u records", (unsigned)s_cap);
        return ESP_ERR_INVALID_STATE;
    }
    /* Each record costs sizeof(heap_trace_record_t) — with a stack depth of 4
     * that is about 52 bytes.  The buffer comes out of the heap we are trying
     * to measure, so the caller picks the size and the answer says what it
     * actually cost. */
    size_t bytes = records * sizeof(heap_trace_record_t);
    if (records == 0 || bytes > 64 * 1024) {
        snprintf(err, errlen, "records out of range (0 < n, buffer <= 64 KB, %u B/record)",
                 (unsigned)sizeof(heap_trace_record_t));
        return ESP_ERR_INVALID_ARG;
    }
    heap_trace_record_t *buf = calloc(records, sizeof(heap_trace_record_t));
    if (!buf) {
        snprintf(err, errlen, "could not allocate %u bytes for the trace buffer",
                 (unsigned)bytes);
        return ESP_ERR_NO_MEM;
    }
    if (heap_trace_init_standalone(buf, records) != ESP_OK ||
        heap_trace_start(HEAP_TRACE_LEAKS) != ESP_OK) {
        free(buf);
        snprintf(err, errlen, "heap_trace refused to start");
        return ESP_FAIL;
    }
    /* The previous window's buffer is kept after a stop so its leaks stay
     * readable; a new start is the moment it goes.  (The first build forgot
     * this and leaked one buffer per restart — the probe leaking is the one
     * thing it must not do.)  The trace module already points at the new
     * buffer, so nothing can read the old one any more. */
    free(s_buf);
    s_buf = buf;
    s_cap = records;
    s_on = true;
    ESP_LOGW(TAG, "heap trace started: %u records, %u bytes of buffer",
             (unsigned)records, (unsigned)bytes);
    return ESP_OK;
}

static void trace_stop(void)
{
    if (!s_on) {
        return;
    }
    heap_trace_stop();
    s_on = false;
    ESP_LOGW(TAG, "heap trace stopped (buffer kept so the leaks stay readable)");
}

/* The leaks grouped two ways.
 *
 *   by_caller  the return-address chain that allocated them.  A list of
 *              individual blocks is unreadable at this scale; what identifies
 *              a leak is one chain showing up hundreds of times.  Resolve the
 *              addresses on the host with addr2line against the same build's
 *              ELF.  Needs CONFIG_HEAP_TRACING_STACK_DEPTH > 0, which on this
 *              target needs the frame pointer (see sdkconfig.defaults.heap).
 *   by_size    a histogram of block sizes, which needs no symbols at all and
 *              is what the census already pointed at: the ~1.2 KB class.
 */
#define MAX_SITES 48
#define CHAIN 3                          /* frames kept per site */
#if CONFIG_HEAP_TRACING_STACK_DEPTH < CHAIN
#undef CHAIN
#define CHAIN CONFIG_HEAP_TRACING_STACK_DEPTH
#endif

static const uint32_t k_size_edges[] = {16, 32, 64, 128, 256, 512, 1024, 1280, 1536, 2048, 4096, 0xffffffff};
#define NBUCKETS (sizeof(k_size_edges) / sizeof(k_size_edges[0]))

static esp_err_t leaks_get(httpd_req_t *req)
{
    if (!s_buf) {
        httpd_resp_set_type(req, "application/json");
        return httpd_resp_sendstr(req, "{\"error\":\"no trace buffer — POST {\\\"trace\\\":\\\"start\\\"} first\"}");
    }
    struct { uint32_t chain[CHAIN > 0 ? CHAIN : 1]; uint32_t count; uint32_t bytes; } site[MAX_SITES];
    int sites = 0;
    uint32_t total = 0, total_bytes = 0, dropped = 0;
    uint32_t bucket_n[NBUCKETS] = {0}, bucket_b[NBUCKETS] = {0};

    size_t n = heap_trace_get_count();
    for (size_t i = 0; i < n; i++) {
        heap_trace_record_t r;
        if (heap_trace_get(i, &r) != ESP_OK) {
            continue;
        }
        total++;
        total_bytes += r.size;
        for (size_t b = 0; b < NBUCKETS; b++) {
            if (r.size <= k_size_edges[b]) {
                bucket_n[b]++;
                bucket_b[b] += r.size;
                break;
            }
        }
        uint32_t chain[CHAIN > 0 ? CHAIN : 1] = {0};
#if CHAIN > 0
        for (int f = 0; f < CHAIN; f++) {
            chain[f] = (uint32_t)(uintptr_t)r.alloced_by[f];
        }
#endif
        int k = 0;
        for (; k < sites; k++) {
            if (memcmp(site[k].chain, chain, sizeof(chain)) == 0) {
                break;
            }
        }
        if (k == sites) {
            if (sites >= MAX_SITES) {
                dropped++;
                continue;
            }
            memcpy(site[sites].chain, chain, sizeof(chain));
            site[sites].count = 0;
            site[sites].bytes = 0;
            sites++;
        }
        site[k].count++;
        site[k].bytes += r.size;
    }
    /* Biggest consumer first — that is the one worth an addr2line. */
    for (int a = 0; a < sites; a++) {
        for (int b = a + 1; b < sites; b++) {
            if (site[b].bytes > site[a].bytes) {
                typeof(site[0]) t = site[a];
                site[a] = site[b];
                site[b] = t;
            }
        }
    }

    httpd_resp_set_type(req, "application/json");
    char head[224];
    snprintf(head, sizeof(head),
             "{\"records\":%" PRIu32 ",\"bytes\":%" PRIu32 ",\"sites\":%d,\"unlisted_sites\":%" PRIu32
             ",\"running\":%s,\"stack_depth\":%d,\"by_size\":[",
             total, total_bytes, sites, dropped, s_on ? "true" : "false",
             (int)CONFIG_HEAP_TRACING_STACK_DEPTH);
    esp_err_t err = httpd_resp_sendstr_chunk(req, head);
    for (size_t b = 0; b < NBUCKETS && err == ESP_OK; b++) {
        char one[96];
        snprintf(one, sizeof(one), "%s{\"upto\":%" PRIu32 ",\"count\":%" PRIu32 ",\"bytes\":%" PRIu32 "}",
                 b ? "," : "", k_size_edges[b], bucket_n[b], bucket_b[b]);
        err = httpd_resp_sendstr_chunk(req, one);
    }
    if (err == ESP_OK) {
        err = httpd_resp_sendstr_chunk(req, "],\"by_caller\":[");
    }
    for (int a = 0; a < sites && err == ESP_OK; a++) {
        char one[192];
        int len = snprintf(one, sizeof(one), "%s{\"chain\":[", a ? "," : "");
        for (int f = 0; f < CHAIN && len < (int)sizeof(one); f++) {
            len += snprintf(one + len, sizeof(one) - len, "%s\"0x%08" PRIx32 "\"",
                            f ? "," : "", site[a].chain[f]);
        }
        if (len < (int)sizeof(one)) {
            snprintf(one + len, sizeof(one) - len, "],\"count\":%" PRIu32 ",\"bytes\":%" PRIu32 "}",
                     site[a].count, site[a].bytes);
        }
        err = httpd_resp_sendstr_chunk(req, one);
    }
    if (err == ESP_OK) {
        err = httpd_resp_sendstr_chunk(req, "]}");
    }
    if (err == ESP_OK) {
        httpd_resp_sendstr_chunk(req, NULL);
    }
    return err;
}

#endif /* CONFIG_HEAP_TRACING_STANDALONE */

static esp_err_t heap_post(httpd_req_t *req)
{
#if !CONFIG_HEAP_TRACING_STANDALONE
    return httpd_resp_send_err(req, HTTPD_501_METHOD_NOT_IMPLEMENTED,
                               "built without CONFIG_HEAP_TRACING_STANDALONE");
#else
    char buf[192];
    int len = req->content_len < (int)sizeof(buf) - 1 ? req->content_len : (int)sizeof(buf) - 1;
    int got = len > 0 ? httpd_req_recv(req, buf, len) : 0;
    if (got < 0) {
        return httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "no body");
    }
    buf[got] = '\0';

    cJSON *root = cJSON_Parse(buf);
    cJSON *act = root ? cJSON_GetObjectItem(root, "trace") : NULL;
    if (!act || !cJSON_IsString(act)) {
        cJSON_Delete(root);
        return httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST,
                                   "expected {\"trace\":\"start\"|\"stop\", \"records\":n}");
    }
    char action[16];
    snprintf(action, sizeof(action), "%s", act->valuestring);
    cJSON *rec = cJSON_GetObjectItem(root, "records");
    size_t records = (rec && cJSON_IsNumber(rec)) ? (size_t)rec->valuedouble : 400;
    cJSON_Delete(root);

    char body[224], err[128] = "";
    if (!strcmp(action, "start")) {
        esp_err_t e = trace_start(records, err, sizeof(err));
        snprintf(body, sizeof(body), "{\"trace\":\"%s\",\"records\":%u,\"error\":\"%s\"}",
                 e == ESP_OK ? "started" : "refused", (unsigned)records, err);
    } else if (!strcmp(action, "stop")) {
        trace_stop();
        snprintf(body, sizeof(body), "{\"trace\":\"stopped\"}");
    } else {
        return httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "trace must be start or stop");
    }
    httpd_resp_set_type(req, "application/json");
    return httpd_resp_sendstr(req, body);
#endif
}

int heap_probe_register(httpd_handle_t server)
{
    int n = 0;
    const httpd_uri_t get_uri  = { .uri = "/heap", .method = HTTP_GET,  .handler = heap_get };
    const httpd_uri_t post_uri = { .uri = "/heap", .method = HTTP_POST, .handler = heap_post };
    if (httpd_register_uri_handler(server, &get_uri) == ESP_OK) {
        n++;
    } else {
        ESP_LOGE(TAG, "could not register GET /heap — raise max_uri_handlers");
    }
    if (httpd_register_uri_handler(server, &post_uri) == ESP_OK) {
        n++;
    } else {
        ESP_LOGE(TAG, "could not register POST /heap — raise max_uri_handlers");
    }
#if CONFIG_HEAP_TRACING_STANDALONE
    const httpd_uri_t leak_uri = { .uri = "/heap/leaks", .method = HTTP_GET, .handler = leaks_get };
    if (httpd_register_uri_handler(server, &leak_uri) == ESP_OK) {
        n++;
    } else {
        ESP_LOGE(TAG, "could not register /heap/leaks — raise max_uri_handlers");
    }
#endif
    ESP_LOGW(TAG, "heap probe active: GET /heap"
#if CONFIG_HEAP_TASK_TRACKING
             " (per task)"
#endif
#if CONFIG_HEAP_TRACING_STANDALONE
             ", GET /heap/leaks, POST /heap {\"trace\":\"start\"}"
#endif
             " — this is an INSTRUMENTED build, not for release");
    return n;
}

#else  /* !CONFIG_THBR_HEAP_PROBE */

int heap_probe_register(httpd_handle_t server)
{
    (void)server;
    return 0;
}

#endif
