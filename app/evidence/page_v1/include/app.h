/* Open replacement application for the Canon MG2550s.
 *
 * This is a standalone app that is flashed with `fwctl flash` into the main
 * application slot (flash 0x10000, RAM 0x20020000). It treats the installed
 * bootloader (stage 0 + stage 2) as read-only: it only reuses the bootloader's
 * leaf drivers at build time and never programs the boot region.
 *
 * It keeps the minimum USB management surface fwctl needs to reflash it
 * (INFO + REBOOT, PID 0x12fe), and adds a USB debug-logging channel so we can
 * observe reverse-engineering experiments (buttons, raw CIS scanner) live. */
#ifndef APP_H
#define APP_H
#include "service.h"   /* pulls in fw.h + protocol.h */

/* Vendor control-IN (bmRequestType 0xC0) request that drains the debug log
 * ring buffer. The host polls it; an empty reply means "nothing buffered". */
#define APP_REQUEST_LOG 0x50u

/* Vendor control-OUT (bmRequestType 0x40, no data) that asks the app to run a
 * one-shot scanner register peek (0x51, read-only) or active write-readback
 * probe (0x52), emitting the result to the debug log. */
#define APP_REQUEST_SCAN 0x51u
#define APP_REQUEST_SCAN_PROBE 0x52u
#define APP_REQUEST_SCAN_BRINGUP 0x53u
#define APP_REQUEST_SCAN_CAPTURE 0x54u
#define APP_REQUEST_SCAN_ENABLE 0x55u
#define APP_REQUEST_SCAN_FIELD 0x56u
#define APP_REQUEST_SCAN_LDMAC 0x57u
#define APP_REQUEST_SCAN_DMASETUP 0x58u
#define APP_REQUEST_SCAN_FULLCAP 0x59u   /* wValue = variant bits */
#define APP_REQUEST_SCAN_DUMP 0x5au      /* control-IN, wValue = 256-byte chunk index */
#define APP_REQUEST_PRINT_TEST 0x60u     /* decode an embedded BJL band, log the result */
#define APP_REQUEST_PRINT_BEGIN 0x61u    /* control-OUT, wValue|wIndex<<16 = total bytes of BJL stream to follow on bulk OUT */
#define APP_REQUEST_BTN 0x70u            /* control-OUT, no data: enable front-panel key probe (logs PANEL+0x28 on change) */
#define APP_REQUEST_MOTOR_JOG 0x73u     /* control-OUT, wValue=dir(0/1); fixed tiny pulse */
#define APP_REQUEST_HOME 0x74u          /* control-OUT, no data: bounded mechanical home */
#define APP_REQUEST_HOME_STATUS 0x75u   /* control-IN: eight little-endian words */
#define APP_REQUEST_MOTOR_STOP 0x76u    /* control-OUT: immediate cancel */
#define APP_REQUEST_PAGE_START 0x77u    /* OUT: wValue rows, wIndex encoder pitch */
#define APP_REQUEST_PAGE_STATUS 0x78u   /* IN: 16 little-endian words */
#define APP_REQUEST_PAGE_DATA 0x79u     /* IN: wValue chunk, wIndex row */
#define APP_REQUEST_PAGE_ACK 0x7au      /* OUT: wValue row; advance after full read */
/* Legacy tuning requests 0x71/0x72 are not supported in this fixed-pulse test. */

/* Scanner (CIS) reverse-engineering support, see src/scanner.c. All bounded;
 * nothing blocks the management loop for long and no flash is written. */
void scanner_request(void);         /* arm a one-shot read-only peek (USB 0x51) */
void scanner_probe_request(void);   /* arm a one-shot config write-readback (USB 0x52) */
void scanner_bringup_request(void); /* arm engine enable + scanner power + reprobe (USB 0x53) */
void scanner_capture_request(void); /* arm a guarded stationary capture (USB 0x54) */
void scanner_enable_request(void);  /* arm enable+mux then re-test dark sub-blocks (USB 0x55) */
void scanner_field_request(void);   /* arm correct-low-field latch test (USB 0x56) */
void scanner_ldmac_request(void);   /* arm LDMAC/BUFADM peek + field write-readback (USB 0x57) */
void scanner_dmasetup_request(void);/* arm LDMAC descriptor programming validation (USB 0x58) */
void scanner_fullcap_request(unsigned variant); /* arm full stationary capture (USB 0x59, wValue=variant) */
unsigned scanner_dump(uint8_t *out, unsigned chunk, unsigned max); /* copy capture buffer (USB 0x5a) */
void print_selftest(void);          /* decode an embedded BJL band, emit result to the debug log */
void print_bulk_begin(unsigned total); /* start a USB bulk print transfer of `total` bytes (USB 0x61) */
void print_bulk_feed(const uint8_t *data, unsigned len); /* feed received bulk bytes; decodes when complete */
bool print_is_active(void);         /* true while a bulk print transfer is in progress (gates bulk OUT arming) */
void buttons_request(void);         /* enable the front-panel key probe (USB 0x70) */
void buttons_poll(void);            /* always-active keys, optional change log */
void scanner_poll(void);            /* run an armed action, if any */
void scanner_lamp_toggle(void);     /* toggle continuous CIS illumination (Cancel key) */
bool scanner_motor_request(unsigned dir);  /* queue fixed pulse, reject overlap/cooldown */
bool scanner_home_request(void);
unsigned scanner_home_status(uint8_t *out);
uint32_t app_clock_ticks(void); /* extends low-29-bit hardware counter deltas */
bool scanner_motor_busy(void);
void scanner_motor_poll(void);             /* drive deadline checked every main loop */
void scanner_motor_cancel(void);
void scanner_motor_shutdown(void);         /* stop and inhibit until reset */
unsigned scanner_home_result(void);
uint32_t scanner_motor_position(void);
uint32_t scanner_motor_reference(void);
bool scanner_motor_page_move(uint32_t position);
bool scanner_motor_move_ok(void);
bool scanner_capture_row(void); /* bounded stationary capture; motor must be off */
void scanner_lamp_off(void);
bool scan_page_request(unsigned rows, unsigned pitch);
bool scan_page_busy(void);
void scan_page_poll(void);
void scan_page_cancel(void);
unsigned scan_page_status(uint8_t *out);
unsigned scan_page_data(uint8_t *out, unsigned row, unsigned chunk, unsigned max);
bool scan_page_ack(unsigned row);

/* Debug logging. Single producer/consumer in the polled main loop, so no
 * locking is needed. dbg_fill() is the USB drain; everything else appends. */
void dbg_init(void);
void dbg_putc(char c);
void dbg_write(const void *data, unsigned length);
void dbg_puts(const char *text);
void dbg_u32(uint32_t value);                  /* 8 lowercase hex digits */
void dbg_hex(const char *label, uint32_t value); /* "label=0x........\n"  */
void dbg_dec(uint32_t value);                  /* unsigned decimal         */
unsigned dbg_fill(uint8_t *out, unsigned max); /* pop up to max bytes      */

#endif
