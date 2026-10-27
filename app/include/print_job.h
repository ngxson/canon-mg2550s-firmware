/* On-device printing: URF raster intake, separation/halftone, band scheduler.
 *
 *   ippusb.c   streams a Print-Job / Send-Document body: IPP attributes are
 *              buffered, the document bytes go to ipp_document_feed()
 *   ipp.c      job identity and state (one job at a time), replies
 *   urf.c      Apple URF (UNIRAST) decoder: pages and rows
 *   print_engine.c  rows -> CMYK bit planes at 600 dpi -> stock band path
 *                   through the hybrid shim (load, prepare, raster, executor,
 *                   advance, eject), owner of the print mechanism for a job */
#ifndef PRINT_JOB_H
#define PRINT_JOB_H
#include "fw.h"

/* ---- urf.c ---- */
#define URF_MAX_WIDTH 2560u            /* pixels per row (A4/Letter at 300 dpi: 2480/2550) */
struct urf_page { unsigned width, height, dpi, components, bpp, index, count; };
struct urf_sink {
    bool (*page)(const struct urf_page *page);                 /* false: cannot take a page now (retry later) */
    bool (*row)(const uint8_t *pixels, unsigned width, unsigned components);   /* false: not accepted, re-offered */
    void (*page_end)(void);
};
struct urf_decoder {
    const struct urf_sink *sink;
    struct urf_page page;
    unsigned stage, need, pos, x, repeat, row_lines, run, fill;
    bool error, done, row_pending, page_pending;
    uint8_t head[32];
    uint8_t pixel[4];
    uint8_t row[URF_MAX_WIDTH * 3u];
};
void urf_init(struct urf_decoder *d, const struct urf_sink *sink);
unsigned urf_feed(struct urf_decoder *d, const uint8_t *data, unsigned len);  /* bytes consumed */
bool urf_flush(struct urf_decoder *d);     /* re-offer pending work; true when nothing is pending */

/* ---- print_engine.c ---- */
enum print_engine_state { PE_IDLE, PE_RECEIVING, PE_PRINTING, PE_COMPLETED, PE_CANCELED, PE_FAILED };
bool print_engine_available(void);         /* the hybrid band path is present on this board */
bool print_engine_busy(void);              /* a job owns the print mechanism (power manager owner) */
bool print_engine_start(void);             /* begin a job; false when busy or unavailable */
unsigned print_engine_feed(const uint8_t *data, unsigned len);   /* URF bytes; returns the accepted count (backpressure) */
void print_engine_finish(void);            /* no more document bytes */
void print_engine_cancel(void);
enum print_engine_state print_engine_state(void);
unsigned print_engine_status(uint8_t *out); /* USB 0xa2: 16 words */
bool print_engine_control(unsigned op, unsigned arg);   /* USB 0xa3: 1 dry mode (wIndex 0/1, emulator), 2 cancel */
void print_engine_poll(void);

/* ---- ipp.c document streaming (called by ippusb.c) ---- */
void ipp_document_begin(unsigned conn, const uint8_t *attributes, unsigned length);
unsigned ipp_document_feed(unsigned conn, const uint8_t *data, unsigned len);
void ipp_document_abort(unsigned conn);

/* Incremental IPP message scanner: finds the end-of-attributes tag so the
 * document data that follows can be streamed instead of buffered. */
struct ipp_scan { unsigned stage, skip; uint16_t length; bool done; };
bool ipp_scan_byte(struct ipp_scan *s, uint8_t b);   /* true once the attributes are complete */

#endif
