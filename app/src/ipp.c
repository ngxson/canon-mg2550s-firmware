/* IPP responder for IPP-USB hosts.
 *
 * IPP-USB hosts (ipp-usb on Linux, the macOS and Windows class drivers) ask
 * Get-Printer-Attributes before they expose a device; the answer supplies the
 * advertised name and UUID. macOS only offers the scanner of a USB device it
 * recognises as an AirPrint printer, so the attributes describe one: URF
 * raster, A4/Letter, 300 dpi. Printing is not implemented yet and the answer
 * says so: printer-is-accepting-jobs is false, job submission is refused with
 * server-error-not-accepting-jobs and there are never any jobs. */
#include "app.h"

enum { TAG_OPERATION = 0x01, TAG_END = 0x03, TAG_PRINTER = 0x04,
       TAG_INTEGER = 0x21, TAG_BOOLEAN = 0x22, TAG_ENUM = 0x23,
       TAG_RESOLUTION = 0x32, TAG_RANGE = 0x33,
       TAG_TEXT = 0x41, TAG_NAME = 0x42, TAG_KEYWORD = 0x44, TAG_URI = 0x45,
       TAG_CHARSET = 0x47, TAG_LANGUAGE = 0x48, TAG_MIME = 0x49 };
enum { OP_PRINT_JOB = 0x0002, OP_VALIDATE_JOB = 0x0004, OP_CREATE_JOB = 0x0005,
       OP_SEND_DOCUMENT = 0x0006, OP_CANCEL_JOB = 0x0008, OP_GET_JOB_ATTRIBUTES = 0x0009,
       OP_GET_JOBS = 0x000a, OP_GET_PRINTER_ATTRIBUTES = 0x000b, OP_IDENTIFY_PRINTER = 0x003c };
#define STATUS_OK 0x0000u
#define STATUS_NOT_FOUND 0x0406u
#define STATUS_NOT_SUPPORTED 0x0501u
#define STATUS_NOT_ACCEPTING 0x0506u

/* One value; a NULL name adds another value to the previous attribute. */
static void attr(struct obuf *o, uint8_t tag, const char *name, const void *value, unsigned length)
{
    unsigned n = name ? (unsigned)strlen(name) : 0;
    uint8_t head[3] = {tag, (uint8_t)(n >> 8), (uint8_t)n};
    uint8_t size[2] = {(uint8_t)(length >> 8), (uint8_t)length};
    ob_mem(o, head, 3); ob_mem(o, name, n);
    ob_mem(o, size, 2); ob_mem(o, value, length);
}

static void group(struct obuf *o, uint8_t tag) { ob_mem(o, &tag, 1); }

static void text(struct obuf *o, uint8_t tag, const char *name, const char *value)
{ attr(o, tag, name, value, (unsigned)strlen(value)); }

/* Several string values of one attribute. */
static void texts(struct obuf *o, uint8_t tag, const char *name, const char *const *values, unsigned count)
{
    for (unsigned i = 0; i < count; ++i) text(o, tag, i ? NULL : name, values[i]);
}

static void be32(uint8_t *out, uint32_t value)
{
    out[0] = (uint8_t)(value >> 24); out[1] = (uint8_t)(value >> 16);
    out[2] = (uint8_t)(value >> 8); out[3] = (uint8_t)value;
}

static void integer(struct obuf *o, uint8_t tag, const char *name, uint32_t value)
{
    uint8_t v[4];
    be32(v, value);
    attr(o, tag, name, v, 4);
}

static void boolean(struct obuf *o, const char *name, bool value)
{
    uint8_t v = value;
    attr(o, TAG_BOOLEAN, name, &v, 1);
}

static void range(struct obuf *o, const char *name, uint32_t low, uint32_t high)
{
    uint8_t v[8];
    be32(v, low); be32(v + 4, high);
    attr(o, TAG_RANGE, name, v, 8);
}

static void resolution(struct obuf *o, const char *name, uint32_t dpi)
{
    uint8_t v[9];
    be32(v, dpi); be32(v + 4, dpi); v[8] = 3;            /* dots per inch */
    attr(o, TAG_RESOLUTION, name, v, 9);
}

/* "scheme://host/path" with the request's Host, as IPP USB requires. */
static void url(struct obuf *o, const char *name, const char *scheme, const char *host, const char *path)
{
    char value[160];
    struct obuf u = {value, 0, sizeof(value), false};
    ob_str(&u, scheme); ob_str(&u, "://"); ob_str(&u, host); ob_str(&u, path);
    attr(o, TAG_URI, name, value, u.n);
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
    group(o, TAG_PRINTER);
    url(o, "printer-uri-supported", "ipp", host, "/ipp/print");
    text(o, TAG_KEYWORD, "uri-authentication-supported", "none");
    text(o, TAG_KEYWORD, "uri-security-supported", "none");
    text(o, TAG_NAME, "printer-name", DEVICE_MAKE_MODEL);
    text(o, TAG_NAME, "printer-dns-sd-name", DEVICE_MAKE_MODEL);
    text(o, TAG_TEXT, "printer-info", DEVICE_MAKE_MODEL);
    text(o, TAG_TEXT, "printer-make-and-model", DEVICE_MAKE_MODEL);
    text(o, TAG_TEXT, "printer-device-id", "MFG:OpenFW;MDL:MG2550s;CMD:URF;CLS:PRINTER;");
    text(o, TAG_URI, "printer-uuid", "urn:uuid:" DEVICE_UUID);
    url(o, "printer-more-info", "http", host, "/");
    text(o, TAG_KEYWORD, "printer-kind", "document");
    integer(o, TAG_ENUM, "printer-state", 3);                 /* idle */
    text(o, TAG_KEYWORD, "printer-state-reasons", "none");
    boolean(o, "printer-is-accepting-jobs", false);
    integer(o, TAG_INTEGER, "printer-up-time", time_ms() / 1000u + 1u);
    integer(o, TAG_INTEGER, "queued-job-count", 0);
    texts(o, TAG_KEYWORD, "ipp-versions-supported", versions, ARRAY_SIZE(versions));
    text(o, TAG_KEYWORD, "ipp-features-supported", "airprint-1.4");
    for (unsigned i = 0; i < ARRAY_SIZE(operations); ++i)
        integer(o, TAG_ENUM, i ? NULL : "operations-supported", operations[i]);
    text(o, TAG_CHARSET, "charset-configured", "utf-8");
    text(o, TAG_CHARSET, "charset-supported", "utf-8");
    text(o, TAG_LANGUAGE, "natural-language-configured", "en");
    text(o, TAG_LANGUAGE, "generated-natural-language-supported", "en");
    text(o, TAG_MIME, "document-format-default", "application/octet-stream");
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

void ipp_handle(unsigned conn, const struct http_request *rq)
{
    const uint8_t *in = rq->body;
    if (rq->body_length < 8u) { http_reply(conn, 400, NULL, NULL, NULL); return; }
    unsigned operation = (unsigned)in[2] << 8 | in[3], status;
    switch (operation) {
    case OP_GET_PRINTER_ATTRIBUTES: case OP_GET_JOBS: case OP_IDENTIFY_PRINTER:
        status = STATUS_OK; break;
    case OP_PRINT_JOB: case OP_VALIDATE_JOB: case OP_CREATE_JOB: case OP_SEND_DOCUMENT:
        status = STATUS_NOT_ACCEPTING; break;
    case OP_CANCEL_JOB: case OP_GET_JOB_ATTRIBUTES:
        status = STATUS_NOT_FOUND; break;                 /* there are never jobs */
    default:
        status = STATUS_NOT_SUPPORTED; break;
    }
    dbg_puts("[ipp] operation 0x"); dbg_u32(operation);
    dbg_puts(" status 0x"); dbg_u32(status); dbg_putc('\n');
    struct obuf o = http_body();
    /* Version and request-id are echoed; request attributes are ignored, so
     * Get-Printer-Attributes always returns the full set. */
    uint8_t head[8] = {in[0], in[1], (uint8_t)(status >> 8), (uint8_t)status, in[4], in[5], in[6], in[7]};
    ob_mem(&o, head, 8);
    group(&o, TAG_OPERATION);
    text(&o, TAG_CHARSET, "attributes-charset", "utf-8");
    text(&o, TAG_LANGUAGE, "attributes-natural-language", "en");
    const char *host = rq->host && *rq->host && strlen(rq->host) < 96u ? rq->host : "localhost";
    if (operation == OP_GET_PRINTER_ATTRIBUTES) printer_attributes(&o, host);
    group(&o, TAG_END);
    http_reply(conn, 200, "application/ipp", NULL, &o);
}
