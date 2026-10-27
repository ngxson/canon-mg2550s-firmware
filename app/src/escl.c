/* eSCL scan service (Apple AirScan / Mopria scan) over IPP-USB.
 *
 * Resources:
 *   GET    /eSCL/ScannerCapabilities          static XML from scan_caps
 *   GET    /eSCL/ScannerStatus                Idle/Processing + the last job
 *   POST   /eSCL/ScanJobs                     ScanSettings -> 201 + Location
 *   GET    /eSCL/ScanJobs/<id>/NextDocument   the page as a chunked PNG stream
 *   GET    /eSCL/ScanJobs/<id>/ScanImageInfo  actual output geometry
 *   DELETE /eSCL/ScanJobs/<id>                cancel
 *
 * One job at a time and one page per job (platen). The source is started when
 * the job is created; rows are encoded and sent as the source delivers them,
 * so the page is never held in memory. A job that is never fetched times out.
 * Formats: JPEG (the default, which Apple's AirScan client relies on when a
 * request names none) and PNG. */
#include "app.h"

#define NS "xmlns:scan=\"http://schemas.hp.com/imaging/escl/2011/05/03\" " \
           "xmlns:pwg=\"http://www.pwg.org/schemas/2010/12/sm\""
#define PENDING_TIMEOUT_MS 60000u
#define STREAM_BATCH 12288u       /* encoded bytes per bulk transfer, roughly (TX_SIZE 16 KiB) */
#define JPEG_QUALITY 85u
#define PRODUCE_BUDGET_MS 40u     /* per call; the converters and encoders yield to the motor every few thousand instructions */

enum doc_format { FORMAT_JPEG, FORMAT_PNG };
static const char *const format_types[] = {"image/jpeg", "image/png"};
enum job_state { JOB_NONE, JOB_PENDING, JOB_PROCESSING, JOB_COMPLETED, JOB_CANCELED, JOB_ABORTED };
static const char *const job_names[] = {"", "Pending", "Processing", "Completed", "Canceled", "Aborted"};
static const char *const job_reasons[] = {"", "JobQueued", "JobScanning", "JobCompletedSuccessfully",
                                          "JobCanceledByUser", "AbortedBySystem"};
static struct {
    enum job_state state;
    unsigned id, rows;
    uint32_t created_ms;
    bool streaming, header_sent;
    struct scan_params params;
    bool bilevel;                 /* BlackAndWhite1: grey source, thresholded */
    enum doc_format format;
} job;
static unsigned next_id = 1;
static struct png_writer png;
static struct jpeg_writer jpeg;
static uint8_t row[SCAN_MAX_PIXELS * 3u];

static bool active(void) { return job.state == JOB_PENDING || job.state == JOB_PROCESSING; }

/* Throughput profile of the streaming path (2026-10-27: a 75 dpi grey page
 * delivered ~10 rows/s while the carriage captured 50 rows/s). Ticks of the
 * 96 MHz clock inside produce(), inside the source row calls, the largest gap
 * between produce() calls (host/USB/main-loop latency) and the wall time. */
static struct { uint32_t calls, produce_ticks, row_ticks, rows, max_gap, last_end, started_ms; } prof;
static void prof_begin(void) { prof.calls = prof.produce_ticks = prof.row_ticks = prof.rows = prof.max_gap = prof.last_end = 0; prof.started_ms = time_ms(); app_loop_max_ticks = app_loop_count = 0; }
static int prof_row(uint8_t *out)
{
    uint32_t t = app_clock_ticks();
    int r = scan_source_row(out);
    prof.row_ticks += app_clock_ticks() - t;
    if (r > 0) ++prof.rows;
    return r;
}

static bool same(const char *a, const char *b)
{
    while (*a && *a == *b) { ++a; ++b; }
    return *a == *b;
}

/* The rest of `path` after `prefix`, or NULL if it does not start with it. */
static const char *after(const char *path, const char *prefix)
{
    while (*prefix && *path == *prefix) { ++path; ++prefix; }
    return *prefix ? NULL : path;
}

/* `path` equals `name`, optionally followed by a query string. */
static bool path_is(const char *path, const char *name)
{
    path = after(path, name);
    return path && (!*path || *path == '?');
}

static void finish(enum job_state state)
{
    if (active() && state != JOB_COMPLETED) scan_source_cancel();
    job.state = state; job.streaming = false;
    dbg_puts("[escl] job "); dbg_dec(job.id); dbg_putc(' ');
    dbg_puts(job_names[state]); dbg_puts(" rows="); dbg_dec(job.rows); dbg_putc('\n');
    if (prof.calls) {
        dbg_puts("[escl] profile calls="); dbg_dec(prof.calls); dbg_puts(" produce_ms="); dbg_dec(prof.produce_ticks / CLOCK_TICKS_PER_MS);
        dbg_puts(" rows_ms="); dbg_dec(prof.row_ticks / CLOCK_TICKS_PER_MS); dbg_puts(" max_gap_ms="); dbg_dec(prof.max_gap / CLOCK_TICKS_PER_MS);
        dbg_puts(" wall_ms="); dbg_dec(time_ms() - prof.started_ms); dbg_puts(" rows="); dbg_dec(prof.rows);
        dbg_puts(" loops="); dbg_dec(app_loop_count); dbg_puts(" loop_max_us="); dbg_dec(app_loop_max_ticks / 96u); dbg_putc('\n');
    }
}

static void element(struct obuf *o, const char *name, const char *text)
{
    ob_str(o, "<"); ob_str(o, name); ob_str(o, ">"); ob_str(o, text);
    ob_str(o, "</"); ob_str(o, name); ob_str(o, ">");
}

static void number(struct obuf *o, const char *name, unsigned value)
{
    ob_str(o, "<"); ob_str(o, name); ob_str(o, ">"); ob_dec(o, value);
    ob_str(o, "</"); ob_str(o, name); ob_str(o, ">");
}

static void job_uri(struct obuf *o) { ob_str(o, "/eSCL/ScanJobs/"); ob_dec(o, job.id); }
static void job_uuid(struct obuf *o) { ob_str(o, "urn:uuid:4f50454e-4657-4a4f-4200-"); ob_hex(o, job.id, 8); ob_str(o, "0000"); }

static void capabilities(unsigned conn)
{
    const struct scan_caps *c = &scan_caps;
    struct obuf o = http_body();
    ob_str(&o, "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<scan:ScannerCapabilities " NS ">");
    element(&o, "pwg:Version", "2.63");
    element(&o, "pwg:MakeAndModel", DEVICE_MAKE_MODEL);
    element(&o, "pwg:SerialNumber", DEVICE_SERIAL);
    element(&o, "scan:UUID", DEVICE_UUID);
    ob_str(&o, "<scan:Platen><scan:PlatenInputCaps>");
    number(&o, "scan:MinWidth", c->min_size); number(&o, "scan:MaxWidth", c->max_width);
    number(&o, "scan:MinHeight", c->min_size); number(&o, "scan:MaxHeight", c->max_height);
    number(&o, "scan:MaxScanRegions", 1);
    ob_str(&o, "<scan:SettingProfiles><scan:SettingProfile><scan:ColorModes>");
    if (c->gray) element(&o, "scan:ColorMode", "BlackAndWhite1");
    if (c->gray) element(&o, "scan:ColorMode", "Grayscale8");
    if (c->color) element(&o, "scan:ColorMode", "RGB24");
    ob_str(&o, "</scan:ColorModes><scan:DocumentFormats>");
    for (unsigned i = 0; i < ARRAY_SIZE(format_types); ++i) element(&o, "pwg:DocumentFormat", format_types[i]);
    for (unsigned i = 0; i < ARRAY_SIZE(format_types); ++i) element(&o, "scan:DocumentFormatExt", format_types[i]);
    ob_str(&o, "</scan:DocumentFormats><scan:SupportedResolutions><scan:DiscreteResolutions>");
    for (unsigned i = 0; i < c->resolution_count; ++i) {
        ob_str(&o, "<scan:DiscreteResolution>");
        number(&o, "scan:XResolution", c->resolutions[i]);
        number(&o, "scan:YResolution", c->resolutions[i]);
        ob_str(&o, "</scan:DiscreteResolution>");
    }
    ob_str(&o, "</scan:DiscreteResolutions></scan:SupportedResolutions>"
               "<scan:ColorSpaces><scan:ColorSpace>sRGB</scan:ColorSpace></scan:ColorSpaces>"
               "</scan:SettingProfile></scan:SettingProfiles><scan:SupportedIntents>"
               "<scan:Intent>Document</scan:Intent><scan:Intent>TextAndGraphic</scan:Intent>"
               "<scan:Intent>Photo</scan:Intent><scan:Intent>Preview</scan:Intent>"
               "</scan:SupportedIntents>");
    number(&o, "scan:MaxOpticalXResolution", c->optical);
    number(&o, "scan:MaxOpticalYResolution", c->optical);
    ob_str(&o, "</scan:PlatenInputCaps></scan:Platen></scan:ScannerCapabilities>\n");
    http_reply(conn, 200, "text/xml", NULL, &o);
}

static void status(unsigned conn)
{
    struct obuf o = http_body();
    ob_str(&o, "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<scan:ScannerStatus " NS ">");
    element(&o, "pwg:Version", "2.63");
    element(&o, "pwg:State", active() ? "Processing" : "Idle");
    if (job.state != JOB_NONE) {
        ob_str(&o, "<scan:Jobs><scan:JobInfo><pwg:JobUri>"); job_uri(&o);
        ob_str(&o, "</pwg:JobUri><pwg:JobUuid>"); job_uuid(&o); ob_str(&o, "</pwg:JobUuid>");
        number(&o, "scan:Age", (time_ms() - job.created_ms) / 1000u);
        number(&o, "pwg:ImagesCompleted", job.state == JOB_COMPLETED);
        number(&o, "pwg:ImagesToTransfer", active());
        element(&o, "pwg:JobState", job_names[job.state]);
        ob_str(&o, "<pwg:JobStateReasons>");
        element(&o, "pwg:JobStateReason", job_reasons[job.state]);
        ob_str(&o, "</pwg:JobStateReasons></scan:JobInfo></scan:Jobs>");
    }
    ob_str(&o, "</scan:ScannerStatus>\n");
    http_reply(conn, 200, "text/xml", NULL, &o);
}

/* Text of the first element whose local name is `name`, ignoring the
 * namespace prefix. ScanSettings is flat enough for this to be unambiguous. */
static bool xml_text(const struct http_request *rq, const char *name,
                     const char **text, unsigned *length)
{
    const char *xml = (const char *)rq->body, *end = xml + rq->body_length;
    unsigned name_length = (unsigned)strlen(name);
    for (const char *p = xml; p < end; ++p) {
        if (!((p - xml) & 255)) app_yield();
        if (*p != '<' || p[1] == '/' || p[1] == '?' || p[1] == '!') continue;
        const char *tag = p + 1, *local = tag, *q = tag;
        while (q < end && *q != '>' && *q != '/' && *q != ' ' && *q != '\t' && *q != '\r' && *q != '\n') {
            if (*q == ':') local = q + 1;
            ++q;
        }
        if ((unsigned)(q - local) != name_length) continue;
        unsigned i = 0;
        while (i < name_length && local[i] == name[i]) ++i;
        if (i != name_length) continue;
        while (q < end && *q != '>') ++q;
        if (q >= end) return false;
        const char *start = q + 1, *stop = start;
        if (q[-1] == '/') stop = start;                  /* empty element */
        else while (stop < end && *stop != '<') ++stop;
        while (start < stop && (*start == ' ' || *start == '\r' || *start == '\n' || *start == '\t')) ++start;
        while (stop > start && (stop[-1] == ' ' || stop[-1] == '\r' || stop[-1] == '\n' || stop[-1] == '\t')) --stop;
        *text = start; *length = (unsigned)(stop - start);
        return true;
    }
    return false;
}

static bool text_is(const char *text, unsigned length, const char *value)
{
    unsigned i = 0;
    while (i < length && value[i] && text[i] == value[i]) ++i;
    return i == length && !value[i];
}

/* Optional numeric element. Absent leaves *value unchanged; junk fails. */
static bool xml_number(const struct http_request *rq, const char *name, unsigned *value)
{
    const char *text; unsigned length, v = 0;
    if (!xml_text(rq, name, &text, &length)) return true;
    if (!length || length > 6u) return false;
    for (unsigned i = 0; i < length; ++i) {
        if (text[i] < '0' || text[i] > '9') return false;
        v = v * 10u + (unsigned)(text[i] - '0');
    }
    *value = v;
    return true;
}

/* Validate ScanSettings against scan_caps. Unsupported values are a conflict
 * rather than being silently substituted, as eSCL expects. Absent elements
 * take defaults: macOS Image Capture's overview scan sends only InputSource,
 * Intent and ColorMode. BlackAndWhite1 is produced from the grey source. */
static bool settings(const struct http_request *rq, struct scan_params *p, bool *bilevel,
                     enum doc_format *format)
{
    const struct scan_caps *c = &scan_caps;
    const char *text; unsigned length;
    unsigned xres = c->resolutions[0], yres = 0;
    *p = (struct scan_params){0, 0, c->max_width, c->max_height, 0, c->color, 0, 0};
    if (xml_text(rq, "InputSource", &text, &length) && !text_is(text, length, "Platen")) return false;
    if (xml_text(rq, "ColorMode", &text, &length)) {
        if (text_is(text, length, "RGB24") && c->color) p->color = true;
        else if (text_is(text, length, "Grayscale8") && c->gray) p->color = false;
        else if (text_is(text, length, "BlackAndWhite1") && c->gray) { p->color = false; *bilevel = true; }
        else return false;
    }
    *format = FORMAT_JPEG;
    if (xml_text(rq, "DocumentFormatExt", &text, &length) || xml_text(rq, "DocumentFormat", &text, &length)) {
        if (text_is(text, length, "image/png")) *format = FORMAT_PNG;
        else if (!text_is(text, length, "image/jpeg")) return false;
    }
    if (xml_text(rq, "ContentRegionUnits", &text, &length)
        && !text_is(text, length, "escl:ThreeHundredthsOfInches")) return false;
    if (!xml_number(rq, "XResolution", &xres) || !xml_number(rq, "YResolution", &yres)
        || !xml_number(rq, "XOffset", &p->x) || !xml_number(rq, "YOffset", &p->y)
        || !xml_number(rq, "Width", &p->width) || !xml_number(rq, "Height", &p->height)) return false;
    if (yres && yres != xres) return false;
    for (unsigned i = 0; i < c->resolution_count; ++i)
        if (c->resolutions[i] == xres) p->resolution = xres;
    if (!p->resolution || p->x >= c->max_width || p->y >= c->max_height) return false;
    if (p->width > c->max_width - p->x) p->width = c->max_width - p->x;
    if (p->height > c->max_height - p->y) p->height = c->max_height - p->y;
    if (p->width < c->min_size || p->height < c->min_size) return false;
    p->pixels = p->width * p->resolution / 300u;
    p->lines = p->height * p->resolution / 300u;
    return p->pixels && p->lines && p->pixels <= SCAN_MAX_PIXELS;
}

/* What the host asked for, before validation: the interoperability trace. */
static void log_settings(const struct http_request *rq)
{
    static const char *const names[] = {"InputSource", "Intent", "ColorMode", "XResolution",
        "DocumentFormatExt", "DocumentFormat", "XOffset", "YOffset", "Width", "Height"};
    dbg_puts("[escl] ScanSettings");
    for (unsigned i = 0; i < ARRAY_SIZE(names); ++i) {
        const char *text; unsigned length;
        dbg_putc(' '); dbg_puts(names[i]); dbg_putc('=');
        if (xml_text(rq, names[i], &text, &length)) dbg_write(text, length < 40u ? length : 40u);
        else dbg_putc('-');
    }
    dbg_putc('\n');
}

static void create_job(unsigned conn, const struct http_request *rq)
{
    struct scan_params p;
    bool bilevel = false;
    enum doc_format format;
    log_settings(rq);
    if (active()) { http_reply(conn, 503, NULL, NULL, NULL); return; }
    if (!settings(rq, &p, &bilevel, &format)) { http_reply(conn, 409, NULL, NULL, NULL); return; }
    prof_begin();
    if (!scan_source_start(&p)) { http_reply(conn, 503, NULL, NULL, NULL); return; }
    job.state = JOB_PENDING; job.id = next_id++; job.rows = 0;
    job.created_ms = time_ms(); job.streaming = job.header_sent = false;
    job.params = p; job.bilevel = bilevel; job.format = format;
    dbg_puts("[escl] job "); dbg_dec(job.id);
    dbg_puts(p.color ? " RGB24 " : bilevel ? " BlackAndWhite1 " : " Grayscale8 ");
    dbg_dec(p.resolution); dbg_puts("dpi "); dbg_dec(p.pixels); dbg_putc('x'); dbg_dec(p.lines);
    dbg_putc(' '); dbg_puts(format_types[format]); dbg_putc('\n');
    char headers[192];
    struct obuf h = {headers, 0, sizeof(headers) - 1u, false};
    ob_str(&h, "Location: ");
    if (rq->host && *rq->host && strlen(rq->host) < 96u) { ob_str(&h, "http://"); ob_str(&h, rq->host); }
    job_uri(&h); ob_str(&h, "\r\n");
    headers[h.n] = 0;
    http_reply(conn, 201, NULL, headers, NULL);
}

/* Threshold a grey row at mid-level and pack it in place, MSB first. */
static void pack_bilevel(uint8_t *pixels, unsigned count)
{
    for (unsigned i = 0; i < count; i += 8u) {
        uint8_t bits = 0;
        if (!(i & 511u)) app_yield();
        for (unsigned b = 0; b < 8u; ++b)
            if (i + b < count && pixels[i + b] >= 128u) bits |= (uint8_t)(0x80u >> b);
        pixels[i / 8u] = bits;
    }
}

/* Bilevel content for JPEG, which has no 1-bit mode: black or white grey. */
static void threshold(uint8_t *pixels, unsigned count)
{
    for (unsigned i = 0; i < count; ++i) {
        if (!(i & 511u)) app_yield();
        pixels[i] = pixels[i] >= 128u ? 255u : 0u;
    }
}

static unsigned produce_jpeg(uint8_t *out, unsigned max, bool *done)
{
    const struct scan_params *p = &job.params;
    unsigned n = 0;
    if (!job.header_sent) {
        n = jpeg_begin(&jpeg, p->pixels, p->lines, p->color, p->resolution, JPEG_QUALITY, out);
        job.header_sent = true;
    }
    uint32_t started = time_ms();
    while (n < STREAM_BATCH && max - n >= JPEG_MCU_MAX && time_ms() - started < PRODUCE_BUDGET_MS) {
        uint8_t *slot = jpeg_row_buffer(&jpeg);
        if (slot) {
            int r = prof_row(slot);
            if (!r) break;
            if (r < 0) { finish(JOB_ABORTED); *done = true; return n; }   /* truncated image */
            if (job.bilevel) threshold(slot, p->pixels);
            jpeg_row_written(&jpeg);
            ++job.rows;
            continue;
        }
        n += jpeg_encode(&jpeg, out + n, max - n);
        if (jpeg.finished) { finish(JOB_COMPLETED); *done = true; break; }
    }
    return n;
}

static unsigned produce_body(uint8_t *out, unsigned max, bool *done);
static unsigned produce(uint8_t *out, unsigned max, bool *done)
{
    uint32_t t = app_clock_ticks();
    if (prof.last_end && t - prof.last_end > prof.max_gap) prof.max_gap = t - prof.last_end;
    unsigned n = produce_body(out, max, done);
    prof.last_end = app_clock_ticks(); prof.produce_ticks += prof.last_end - t; ++prof.calls;
    return n;
}

static unsigned produce_body(uint8_t *out, unsigned max, bool *done)
{
    unsigned n = 0;
    const struct scan_params *p = &job.params;
    if (job.state != JOB_PROCESSING) { job.streaming = false; *done = true; return 0; }
    if (job.format == FORMAT_JPEG) return produce_jpeg(out, max, done);
    if (!job.header_sent) {
        enum png_format format = p->color ? PNG_RGB : job.bilevel ? PNG_BILEVEL : PNG_GRAY;
        n = png_begin(&png, p->pixels, p->lines, format, p->resolution, out);
        job.header_sent = true;
    }
    uint32_t started = time_ms();
    while (job.rows < p->lines && n < STREAM_BATCH && max - n >= png.row_bytes + PNG_ROW_OVERHEAD
           && time_ms() - started < PRODUCE_BUDGET_MS) {
        int r = prof_row(row);
        if (!r) break;
        if (r < 0) { finish(JOB_ABORTED); *done = true; return n; }   /* truncated image */
        if (job.bilevel) pack_bilevel(row, p->pixels);
        n += png_row(&png, row, out + n);
        ++job.rows;
    }
    if (job.rows == p->lines) { finish(JOB_COMPLETED); *done = true; }
    return n;
}

static void stream_abort(void)
{
    if (job.state == JOB_PROCESSING) finish(JOB_ABORTED);
}

static const struct http_stream image_stream = {produce, stream_abort};

static void next_document(unsigned conn)
{
    if (job.state != JOB_PENDING) { http_reply(conn, 404, NULL, NULL, NULL); return; }
    if (job.streaming) { http_reply(conn, 503, NULL, NULL, NULL); return; }
    job.state = JOB_PROCESSING; job.streaming = true;
    http_stream(conn, 200, format_types[job.format], &image_stream);
}

static void image_info(unsigned conn)
{
    const struct scan_params *p = &job.params;
    struct obuf o = http_body();
    ob_str(&o, "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<scan:ScanImageInfo " NS "><pwg:JobUri>");
    job_uri(&o); ob_str(&o, "</pwg:JobUri><pwg:JobUuid>"); job_uuid(&o); ob_str(&o, "</pwg:JobUuid>");
    number(&o, "scan:ActualWidth", p->pixels);
    number(&o, "scan:ActualHeight", p->lines);
    number(&o, "scan:ActualBytesPerLine",
           p->color ? p->pixels * 3u : job.bilevel ? (p->pixels + 7u) / 8u : p->pixels);
    ob_str(&o, "</scan:ScanImageInfo>\n");
    http_reply(conn, 200, "text/xml", NULL, &o);
}

static void job_request(unsigned conn, const struct http_request *rq, const char *path)
{
    unsigned id = 0;
    while (*path >= '0' && *path <= '9' && id < 100000000u) id = id * 10u + (unsigned)(*path++ - '0');
    if (job.state == JOB_NONE || id != job.id) http_reply(conn, 404, NULL, NULL, NULL);
    else if (same(rq->method, "DELETE") && path_is(path, "")) {
        if (active()) finish(JOB_CANCELED);
        http_reply(conn, 200, NULL, NULL, NULL);
    } else if (same(rq->method, "GET") && path_is(path, "/NextDocument")) next_document(conn);
    else if (same(rq->method, "GET") && path_is(path, "/ScanImageInfo")) image_info(conn);
    else http_reply(conn, 404, NULL, NULL, NULL);
}

void escl_handle(unsigned conn, const struct http_request *rq)
{
    const char *path = rq->path + 5;                     /* after "/eSCL" */
    bool get = same(rq->method, "GET");
    if (path_is(path, "/ScannerCapabilities")) {
        if (get) capabilities(conn); else http_reply(conn, 405, NULL, NULL, NULL);
    } else if (path_is(path, "/ScannerStatus")) {
        if (get) status(conn); else http_reply(conn, 405, NULL, NULL, NULL);
    } else if (path_is(path, "/ScanJobs")) {
        if (same(rq->method, "POST")) create_job(conn, rq); else http_reply(conn, 405, NULL, NULL, NULL);
    } else if (after(path, "/ScanJobs/")) job_request(conn, rq, after(path, "/ScanJobs/"));
    else http_reply(conn, 404, NULL, NULL, NULL);
}

void escl_poll(void)
{
    if (job.state == JOB_PENDING && time_ms() - job.created_ms > PENDING_TIMEOUT_MS)
        finish(JOB_ABORTED);
}
