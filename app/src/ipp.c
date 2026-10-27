/* IPP responder for IPP-USB hosts, with one print job at a time.
 *
 * IPP-USB hosts (ipp-usb on Linux, the macOS and Windows class drivers) ask
 * Get-Printer-Attributes before they expose a device; the answer supplies the
 * advertised name and UUID and describes an AirPrint printer: URF raster
 * (W8 grey or SRGB24), A4/Letter, 300 dpi. Print-Job, or Create-Job followed
 * by Send-Document, hands the document to print_engine.c as it streams in
 * (ippusb.c calls ipp_document_begin/feed while receiving); the reply carries
 * the job id and state, and Get-Jobs / Get-Job-Attributes / Cancel-Job work
 * on that job until the next one replaces it. Boards without the hybrid band
 * path (print_engine_available() false) refuse jobs with
 * server-error-not-accepting-jobs as before. */
#include "app.h"
#include "print_job.h"

enum { TAG_OPERATION = 0x01, TAG_JOB = 0x02, TAG_END = 0x03, TAG_PRINTER = 0x04,
       TAG_INTEGER = 0x21, TAG_BOOLEAN = 0x22, TAG_ENUM = 0x23,
       TAG_RESOLUTION = 0x32, TAG_RANGE = 0x33,
       TAG_TEXT = 0x41, TAG_NAME = 0x42, TAG_KEYWORD = 0x44, TAG_URI = 0x45,
       TAG_CHARSET = 0x47, TAG_LANGUAGE = 0x48, TAG_MIME = 0x49 };
enum { OP_PRINT_JOB = 0x0002, OP_VALIDATE_JOB = 0x0004, OP_CREATE_JOB = 0x0005,
       OP_SEND_DOCUMENT = 0x0006, OP_CANCEL_JOB = 0x0008, OP_GET_JOB_ATTRIBUTES = 0x0009,
       OP_GET_JOBS = 0x000a, OP_GET_PRINTER_ATTRIBUTES = 0x000b, OP_IDENTIFY_PRINTER = 0x003c };
#define STATUS_OK 0x0000u
#define STATUS_BAD_REQUEST 0x0400u
#define STATUS_NOT_FOUND 0x0406u
#define STATUS_NOT_POSSIBLE 0x040bu       /* client-error-not-possible */
#define STATUS_FORMAT 0x040au             /* client-error-document-format-not-supported */
#define STATUS_NOT_SUPPORTED 0x0501u
#define STATUS_BUSY 0x0507u               /* server-error-busy */
#define STATUS_NOT_ACCEPTING 0x0506u
enum { JS_PENDING = 3, JS_PROCESSING = 5, JS_CANCELED = 7, JS_ABORTED = 8, JS_COMPLETED = 9 };

static struct { unsigned id; bool exists, created_only; } job;   /* the one job; created_only: Create-Job without a document yet */
static unsigned next_job_id = 1;
/* Per connection: the document streamed with the request being received. */
static struct { bool active, accepted; unsigned status, op; } doc[IPPUSB_INTERFACES];

/* ---- encoding ---- */
static void attr(struct obuf *o, uint8_t tag, const char *name, const void *value, unsigned length)
{
    unsigned n = name ? (unsigned)strlen(name) : 0;
    uint8_t head[3] = {tag, (uint8_t)(n >> 8), (uint8_t)n};
    uint8_t size[2] = {(uint8_t)(length >> 8), (uint8_t)length};
    ob_mem(o, head, 3); ob_mem(o, name, n);
    ob_mem(o, size, 2); ob_mem(o, value, length);
}
static void group(struct obuf *o, uint8_t tag) { ob_mem(o, &tag, 1); }
static void text(struct obuf *o, uint8_t tag, const char *name, const char *value) { attr(o, tag, name, value, (unsigned)strlen(value)); }
static void texts(struct obuf *o, uint8_t tag, const char *name, const char *const *values, unsigned count)
{
    for (unsigned i = 0; i < count; ++i) text(o, tag, i ? NULL : name, values[i]);
}
static void be32(uint8_t *out, uint32_t value)
{
    out[0] = (uint8_t)(value >> 24); out[1] = (uint8_t)(value >> 16); out[2] = (uint8_t)(value >> 8); out[3] = (uint8_t)value;
}
static uint32_t rd32(const uint8_t *p) { return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3]; }
static void integer(struct obuf *o, uint8_t tag, const char *name, uint32_t value) { uint8_t v[4]; be32(v, value); attr(o, tag, name, v, 4); }
static void boolean(struct obuf *o, const char *name, bool value) { uint8_t v = value; attr(o, TAG_BOOLEAN, name, &v, 1); }
static void range(struct obuf *o, const char *name, uint32_t low, uint32_t high) { uint8_t v[8]; be32(v, low); be32(v + 4, high); attr(o, TAG_RANGE, name, v, 8); }
static void resolution(struct obuf *o, const char *name, uint32_t dpi) { uint8_t v[9]; be32(v, dpi); be32(v + 4, dpi); v[8] = 3; attr(o, TAG_RESOLUTION, name, v, 9); }
static void url(struct obuf *o, const char *name, const char *scheme, const char *host, const char *path, unsigned id)
{
    char value[160];
    struct obuf u = {value, 0, sizeof(value), false};
    ob_str(&u, scheme); ob_str(&u, "://"); ob_str(&u, host); ob_str(&u, path);
    if (id) ob_dec(&u, id);
    attr(o, TAG_URI, name, value, u.n);
}

/* ---- request attributes (operation group) ---- */
struct request_attrs { const uint8_t *format; unsigned format_len; unsigned job_id; bool last_document, has_last; };

static bool same_n(const uint8_t *a, unsigned n, const char *b)
{
    for (unsigned i = 0; i < n; ++i) { char c = (char)a[i]; if (c >= 'A' && c <= 'Z') c = (char)(c + 32); if (c != b[i]) return false; }
    return !b[n];
}

static void parse_attrs(const uint8_t *in, unsigned len, struct request_attrs *a)
{
    *a = (struct request_attrs){0};
    unsigned pos = 8;
    const uint8_t *name = NULL; unsigned name_len = 0;
    while (pos < len) {
        uint8_t tag = in[pos++];
        if (tag == TAG_END) break;
        if (tag < 0x10) continue;                               /* group delimiter */
        if (pos + 2 > len) break;
        unsigned nl = (unsigned)in[pos] << 8 | in[pos + 1]; pos += 2;
        if (nl) { name = in + pos; name_len = nl; pos += nl; }  /* nl == 0: another value of the previous attribute */
        if (pos + 2 > len) break;
        unsigned vl = (unsigned)in[pos] << 8 | in[pos + 1]; pos += 2;
        if (pos + vl > len) break;
        const uint8_t *v = in + pos; pos += vl;
        if (!name) continue;
        if (same_n(name, name_len, "document-format")) { a->format = v; a->format_len = vl; }
        else if (same_n(name, name_len, "job-id") && vl == 4) a->job_id = rd32(v);
        else if (same_n(name, name_len, "last-document") && vl == 1) { a->last_document = v[0] != 0; a->has_last = true; }
    }
}

static bool format_ok(const struct request_attrs *a)
{
    if (!a->format) return true;                                 /* default: application/octet-stream, sniffed by the decoder */
    return same_n(a->format, a->format_len, "image/urf") || same_n(a->format, a->format_len, "application/octet-stream");
}

/* ---- job state ---- */
static unsigned job_state(void)
{
    if (job.created_only) return JS_PENDING;
    switch (print_engine_state()) {
    case PE_RECEIVING: case PE_PRINTING: return JS_PROCESSING;
    case PE_COMPLETED: return JS_COMPLETED;
    case PE_CANCELED: return JS_CANCELED;
    default: return JS_ABORTED;
    }
}
static const char *job_reason(void)
{
    switch (job_state()) {
    case JS_PENDING: return "job-incoming";
    case JS_PROCESSING: return print_engine_state() == PE_RECEIVING ? "job-incoming" : "job-printing";
    case JS_COMPLETED: return "job-completed-successfully";
    case JS_CANCELED: return "job-canceled-by-user";
    default: return "aborted-by-system";
    }
}

static void job_attributes(struct obuf *o, const char *host)
{
    group(o, TAG_JOB);
    integer(o, TAG_INTEGER, "job-id", job.id);
    url(o, "job-uri", "ipp", host, "/ipp/print/job/", job.id);
    url(o, "job-printer-uri", "ipp", host, "/ipp/print", 0);
    integer(o, TAG_ENUM, "job-state", job_state());
    text(o, TAG_KEYWORD, "job-state-reasons", job_reason());
    text(o, TAG_NAME, "job-name", "document");
    text(o, TAG_NAME, "job-originating-user-name", "user");
}

static void printer_attributes(struct obuf *o, const char *host)
{
    static const char *const versions[] = {"1.1", "2.0"};
    static const char *const formats[] = {"application/octet-stream", "image/urf"};
    static const char *const urf[] = {"W8", "SRGB24", "CP1", "RS300", "V1.4", "DM1"};
    static const char *const media[] = {"iso_a4_210x297mm", "na_letter_8.5x11in"};
    static const char *const color_modes[] = {"monochrome", "color"};
    static const uint16_t operations[] = {OP_PRINT_JOB, OP_VALIDATE_JOB, OP_CREATE_JOB, OP_SEND_DOCUMENT,
        OP_CANCEL_JOB, OP_GET_JOB_ATTRIBUTES, OP_GET_JOBS, OP_GET_PRINTER_ATTRIBUTES, OP_IDENTIFY_PRINTER};
    bool accepting = print_engine_available();
    bool busy = print_engine_busy() || job.created_only;
    group(o, TAG_PRINTER);
    url(o, "printer-uri-supported", "ipp", host, "/ipp/print", 0);
    text(o, TAG_KEYWORD, "uri-authentication-supported", "none");
    text(o, TAG_KEYWORD, "uri-security-supported", "none");
    text(o, TAG_NAME, "printer-name", DEVICE_MAKE_MODEL);
    text(o, TAG_NAME, "printer-dns-sd-name", DEVICE_MAKE_MODEL);
    text(o, TAG_TEXT, "printer-info", DEVICE_MAKE_MODEL);
    text(o, TAG_TEXT, "printer-make-and-model", DEVICE_MAKE_MODEL);
    text(o, TAG_TEXT, "printer-device-id", "MFG:OpenFW;MDL:MG2550s;CMD:URF;CLS:PRINTER;");
    text(o, TAG_URI, "printer-uuid", "urn:uuid:" DEVICE_UUID);
    url(o, "printer-more-info", "http", host, "/", 0);
    text(o, TAG_KEYWORD, "printer-kind", "document");
    integer(o, TAG_ENUM, "printer-state", busy ? 4u : 3u);     /* processing / idle */
    text(o, TAG_KEYWORD, "printer-state-reasons", "none");
    boolean(o, "printer-is-accepting-jobs", accepting);
    integer(o, TAG_INTEGER, "printer-up-time", time_ms() / 1000u + 1u);
    integer(o, TAG_INTEGER, "queued-job-count", busy ? 1u : 0u);
    texts(o, TAG_KEYWORD, "ipp-versions-supported", versions, ARRAY_SIZE(versions));
    text(o, TAG_KEYWORD, "ipp-features-supported", "airprint-1.4");
    for (unsigned i = 0; i < ARRAY_SIZE(operations); ++i)
        integer(o, TAG_ENUM, i ? NULL : "operations-supported", operations[i]);
    text(o, TAG_CHARSET, "charset-configured", "utf-8");
    text(o, TAG_CHARSET, "charset-supported", "utf-8");
    text(o, TAG_LANGUAGE, "natural-language-configured", "en");
    text(o, TAG_LANGUAGE, "generated-natural-language-supported", "en");
    text(o, TAG_MIME, "document-format-default", "image/urf");
    texts(o, TAG_MIME, "document-format-supported", formats, ARRAY_SIZE(formats));
    texts(o, TAG_KEYWORD, "urf-supported", urf, ARRAY_SIZE(urf));
    text(o, TAG_KEYWORD, "pdl-override-supported", "attempted");
    text(o, TAG_KEYWORD, "compression-supported", "none");
    boolean(o, "color-supported", true);
    text(o, TAG_KEYWORD, "print-color-mode-default", "color");
    texts(o, TAG_KEYWORD, "print-color-mode-supported", color_modes, ARRAY_SIZE(color_modes));
    text(o, TAG_KEYWORD, "media-default", media[0]);
    texts(o, TAG_KEYWORD, "media-supported", media, ARRAY_SIZE(media));
    texts(o, TAG_KEYWORD, "media-ready", media, 1);
    text(o, TAG_KEYWORD, "media-type-supported", "stationery");
    text(o, TAG_KEYWORD, "media-source-supported", "main");
    text(o, TAG_KEYWORD, "sides-default", "one-sided");
    text(o, TAG_KEYWORD, "sides-supported", "one-sided");
    integer(o, TAG_INTEGER, "copies-default", 1);
    range(o, "copies-supported", 1, 1);
    resolution(o, "printer-resolution-default", 300);
    resolution(o, "printer-resolution-supported", 300);
    integer(o, TAG_ENUM, "print-quality-default", 4);         /* normal */
    integer(o, TAG_ENUM, "print-quality-supported", 4);
    integer(o, TAG_ENUM, "orientation-requested-default", 3); /* portrait */
    integer(o, TAG_ENUM, "orientation-requested-supported", 3);
    integer(o, TAG_ENUM, "finishings-default", 3);            /* none */
    integer(o, TAG_ENUM, "finishings-supported", 3);
    text(o, TAG_KEYWORD, "multiple-document-handling-supported", "separate-documents-uncollated-copies");
}

/* ---- document streaming (ippusb.c) ---- */
/* The attributes of a request with a document body have arrived; decide now
 * whether the document goes to the engine or is drained. */
void ipp_document_begin(unsigned conn, const uint8_t *attributes, unsigned length)
{
    struct request_attrs a;
    unsigned op = length >= 4 ? (unsigned)attributes[2] << 8 | attributes[3] : 0;
    doc[conn].active = true; doc[conn].accepted = false; doc[conn].status = STATUS_NOT_ACCEPTING; doc[conn].op = op;
    if (op != OP_PRINT_JOB && op != OP_SEND_DOCUMENT) { doc[conn].status = STATUS_OK; return; }   /* no document expected */
    parse_attrs(attributes, length, &a);
    if (!print_engine_available()) { doc[conn].status = STATUS_NOT_ACCEPTING; return; }
    if (!format_ok(&a)) { doc[conn].status = STATUS_FORMAT; return; }
    if (op == OP_SEND_DOCUMENT) {
        if (!job.exists || a.job_id != job.id) { doc[conn].status = STATUS_NOT_FOUND; return; }
        if (!job.created_only || !print_engine_start()) { doc[conn].status = STATUS_NOT_POSSIBLE; return; }
        job.created_only = false;
    } else {
        if (print_engine_busy() || job.created_only) { doc[conn].status = STATUS_BUSY; return; }
        if (!print_engine_start()) { doc[conn].status = STATUS_BUSY; return; }
        job.exists = true; job.created_only = false; job.id = next_job_id++;
    }
    doc[conn].accepted = true; doc[conn].status = STATUS_OK;
    dbg_puts("[ipp] job "); dbg_dec(job.id); dbg_puts(" document streaming\n");
}

unsigned ipp_document_feed(unsigned conn, const uint8_t *data, unsigned len)
{
    if (!doc[conn].accepted) return len;                       /* drained */
    return print_engine_feed(data, len);
}

void ipp_document_abort(unsigned conn)
{
    if (doc[conn].accepted) { dbg_puts("[ipp] document aborted by the host\n"); print_engine_cancel(); }
    doc[conn].active = doc[conn].accepted = false;
}

/* ---- requests ---- */
void ipp_handle(unsigned conn, const struct http_request *rq)
{
    const uint8_t *in = rq->body;
    if (rq->body_length < 8u) { doc[conn].active = false; http_reply(conn, 400, NULL, NULL, NULL); return; }
    unsigned operation = (unsigned)in[2] << 8 | in[3], status = STATUS_OK;
    struct request_attrs a; parse_attrs(in, rq->body_length, &a);
    bool with_job = false;
    switch (operation) {
    case OP_GET_PRINTER_ATTRIBUTES: case OP_IDENTIFY_PRINTER:
        break;
    case OP_GET_JOBS:
        with_job = job.exists && (job.created_only || print_engine_busy());
        break;
    case OP_VALIDATE_JOB:
        status = !print_engine_available() ? STATUS_NOT_ACCEPTING : !format_ok(&a) ? STATUS_FORMAT : STATUS_OK;
        break;
    case OP_CREATE_JOB:
        if (!print_engine_available()) status = STATUS_NOT_ACCEPTING;
        else if (print_engine_busy() || job.created_only) status = STATUS_BUSY;
        else { job.exists = true; job.created_only = true; job.id = next_job_id++; with_job = true; }
        break;
    case OP_PRINT_JOB: case OP_SEND_DOCUMENT:
        status = doc[conn].active ? doc[conn].status : STATUS_BAD_REQUEST;
        if (doc[conn].accepted) {
            if (operation == OP_PRINT_JOB || !a.has_last || a.last_document) print_engine_finish();
            with_job = true;
        } else if (operation == OP_SEND_DOCUMENT && status == STATUS_OK) {   /* an empty final Send-Document closes a created job */
            if (job.exists && a.job_id == job.id && job.created_only) { job.created_only = false; status = STATUS_NOT_POSSIBLE; }
            else status = STATUS_NOT_FOUND;
        }
        break;
    case OP_CANCEL_JOB:
        if (!job.exists || a.job_id != job.id) status = STATUS_NOT_FOUND;
        else if (job.created_only) job.created_only = false;
        else if (print_engine_busy()) print_engine_cancel();
        else status = STATUS_NOT_POSSIBLE;                       /* already finished */
        break;
    case OP_GET_JOB_ATTRIBUTES:
        if (!job.exists || a.job_id != job.id) status = STATUS_NOT_FOUND; else with_job = true;
        break;
    default:
        status = STATUS_NOT_SUPPORTED; break;
    }
    doc[conn].active = doc[conn].accepted = false;
    dbg_puts("[ipp] operation 0x"); dbg_u32(operation);
    dbg_puts(" status 0x"); dbg_u32(status); dbg_putc('\n');
    struct obuf o = http_body();
    uint8_t head[8] = {in[0], in[1], (uint8_t)(status >> 8), (uint8_t)status, in[4], in[5], in[6], in[7]};
    ob_mem(&o, head, 8);
    group(&o, TAG_OPERATION);
    text(&o, TAG_CHARSET, "attributes-charset", "utf-8");
    text(&o, TAG_LANGUAGE, "attributes-natural-language", "en");
    const char *host = rq->host && *rq->host && strlen(rq->host) < 96u ? rq->host : "localhost";
    if (operation == OP_GET_PRINTER_ATTRIBUTES) printer_attributes(&o, host);
    if (with_job) job_attributes(&o, host);
    group(&o, TAG_END);
    http_reply(conn, 200, "application/ipp", NULL, &o);
}

/* ---- incremental attribute scanner (ippusb.c) ---- */
bool ipp_scan_byte(struct ipp_scan *s, uint8_t b)
{
    /* stages: 0..7 header; 8 tag; 9/10 name length; 11 name bytes; 12/13 value length; 14 value bytes */
    if (s->done) return true;
    switch (s->stage) {
    case 8:
        if (b == TAG_END) { s->done = true; return true; }
        if (b < 0x10) return false;                               /* group delimiter */
        s->stage = 9; return false;
    case 9: s->length = (uint16_t)(b << 8); s->stage = 10; return false;
    case 10: s->length |= b; s->skip = s->length; s->stage = s->skip ? 11 : 12; return false;
    case 11: if (!--s->skip) s->stage = 12; return false;
    case 12: s->length = (uint16_t)(b << 8); s->stage = 13; return false;
    case 13: s->length |= b; s->skip = s->length; s->stage = s->skip ? 14 : 8; return false;
    case 14: if (!--s->skip) s->stage = 8; return false;
    default: if (++s->stage == 8) {} return false;              /* 8 header bytes */
    }
}
