/* Driverless scanning over USB: IPP-over-USB transport, HTTP/1.1 framing,
 * the eSCL scan service, a minimal IPP identity and a streaming PNG writer.
 *
 * Layering (each file only talks to the one below it):
 *   usb.c            bulk endpoints of the two IPP-USB interfaces (class 7/1/4)
 *   ippusb.c         one HTTP/1.1 byte stream per interface, request routing
 *   escl.c / ipp.c   eSCL resources (/eSCL/...) and IPP Get-Printer-Attributes
 *   png.c, jpeg.c    row-by-row PNG / strip-by-strip baseline JPEG encoding
 *   scan source      the image provider: scan_source_*() below
 *
 * The scan source is the only part that knows about hardware. The current one
 * (scan_source_dummy.c) draws a synthetic test chart; the scanner path will
 * implement the same three functions and the scan_caps table. */
#ifndef ESCL_H
#define ESCL_H
#include "fw.h"

#define IPPUSB_INTERFACES 2u
#define DEVICE_MAKE_MODEL "OpenFW MG2550s"
#define DEVICE_SERIAL "OPEN0001"
#define DEVICE_UUID "4f50454e-4657-4d47-3235-353073000001"

/* usb.c: bulk access for IPP-USB interface `conn`. Both return false while
 * the device is not configured. A receive arms exactly one max-size packet. */
bool usb_ipp_send(unsigned conn, const uint8_t *data, unsigned length);
bool usb_ipp_receive(unsigned conn, uint8_t *packet);
unsigned usb_max_packet(void);

/* ippusb.c: called by usb.c on bus events and endpoint completions. */
void ippusb_reset(void);                         /* bus reset or deconfigure */
void ippusb_reset_conn(unsigned conn);           /* SET_INTERFACE / SOFT_RESET */
void ippusb_received(unsigned conn, unsigned length);
void ippusb_sent(unsigned conn);
void ippusb_poll(void);                          /* main loop */

/* Bounded text/binary builder. Overflow truncates and is reported. */
struct obuf { char *p; unsigned n, cap; bool overflow; };
void ob_mem(struct obuf *o, const void *data, unsigned length);
void ob_str(struct obuf *o, const char *text);
void ob_dec(struct obuf *o, unsigned value);
void ob_hex(struct obuf *o, uint32_t value, unsigned digits);

/* A parsed request. Strings and body stay valid only during the handler. */
struct http_request {
    const char *method, *path, *host, *content_type;
    const uint8_t *body;        /* NUL-terminated for convenience */
    unsigned body_length;
};
struct http_stream {
    /* Fill at most `max` bytes. Return 0 with *done false when nothing is
     * ready yet; set *done on the final call. Framing is added by ippusb.c. */
    unsigned (*produce)(uint8_t *out, unsigned max, bool *done);
    void (*abort)(void);        /* the connection was reset mid-stream */
};
struct obuf http_body(void);    /* shared scratch for a complete reply body */
void http_reply(unsigned conn, unsigned status, const char *type,
                const char *headers, const struct obuf *body);
void http_stream(unsigned conn, unsigned status, const char *type,
                 const struct http_stream *stream);

void escl_handle(unsigned conn, const struct http_request *request);
void escl_poll(void);
void ipp_handle(unsigned conn, const struct http_request *request);

/* png.c: uncompressed (stored) deflate blocks. Bilevel rows are packed, most
 * significant bit first, 1 = white. */
enum png_format { PNG_BILEVEL, PNG_GRAY, PNG_RGB };
#define PNG_HEADER_BYTES 54u
#define PNG_ROW_OVERHEAD 40u    /* IDAT + zlib framing; final row adds IEND */
struct png_writer { unsigned width, height, row_bytes, row; uint32_t adler; };
unsigned png_begin(struct png_writer *png, unsigned width, unsigned height,
                   enum png_format format, unsigned dpi, uint8_t *out);
unsigned png_row(struct png_writer *png, const uint8_t *pixels, uint8_t *out);

/* jpeg.c: baseline JFIF, greyscale or YCbCr 4:4:4. Feed rows into the buffer
 * jpeg_row_buffer returns (NULL while a full strip awaits encoding), then
 * jpeg_encode writes as many MCUs as fit, and EOI after the last one. */
#define JPEG_HEADER_MAX 640u
#define JPEG_MCU_MAX 1600u      /* worst case for one MCU incl. stuffing, EOI */
struct jpeg_writer {
    unsigned width, height, components, top, strip_rows, mcu, count;
    uint32_t acc;
    int dc[3];
    bool finished;
};
unsigned jpeg_begin(struct jpeg_writer *j, unsigned width, unsigned height, bool color,
                    unsigned dpi, unsigned quality, uint8_t *out);
uint8_t *jpeg_row_buffer(struct jpeg_writer *j);
void jpeg_row_written(struct jpeg_writer *j);
unsigned jpeg_encode(struct jpeg_writer *j, uint8_t *out, unsigned max);

/* Scan source. Geometry is in 1/300 inch, as eSCL uses. Resolutions must
 * divide 300 so a pixel maps to a whole number of those units. */
#define SCAN_MAX_PIXELS 2550u   /* widest row at the highest resolution */
struct scan_params {
    unsigned x, y, width, height;  /* region, 1/300 inch */
    unsigned resolution;           /* dpi, same in both directions */
    bool color;                    /* RGB24, otherwise Grayscale8 */
    unsigned pixels, lines;        /* output size */
};
struct scan_caps {
    unsigned max_width, max_height, min_size, optical;
    const uint16_t *resolutions;
    unsigned resolution_count;
    bool gray, color;
};
extern const struct scan_caps scan_caps;
bool scan_source_start(const struct scan_params *params);
int scan_source_row(uint8_t *pixels);  /* 1 row written, 0 not ready, <0 failed */
void scan_source_cancel(void);
bool scan_source_set_order(unsigned r, unsigned g, unsigned b);   /* scanner source: illumination phase -> R, G, B */

#endif
