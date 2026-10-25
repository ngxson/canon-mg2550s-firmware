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
#include "escl.h"      /* IPP-USB / eSCL driverless scanning */

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
#define APP_REQUEST_SCAN_BURST 0x5bu     /* OUT: wValue = n | exposure_eighths << 8 | offset_step << 12,
                                            wIndex = gain | skip/8 << 8; AFE R1/R4 = 0xff - 0x20*step */
#define APP_REQUEST_BURST_DUMP 0x5cu     /* IN: wValue = 256-byte chunk of the burst buffer */
#define APP_REQUEST_PAGE_PARK 0x7bu      /* OUT: wValue = offset / 256 counts; home, move, stay */
#define APP_REQUEST_PRINT_TEST 0x60u     /* decode an embedded BJL band, log the result */
#define APP_REQUEST_PRINT_BEGIN 0x61u    /* control-OUT, wValue|wIndex<<16 = total bytes of BJL stream to follow on bulk OUT */
#define APP_REQUEST_CART_READ 0x62u      /* control-OUT: wValue = head mask (1 black, 2 colour), wIndex = wait per sample in 10 us (0 = 1 ms) */
#define APP_REQUEST_CART_STATUS 0x63u    /* IN: 16 words, see src/cartridge.c */
#define APP_REQUEST_PRINT_REGS 0x64u     /* IN: read-only CR/LF motor register dump, see src/print_mech.c */
#define APP_REQUEST_PRINT_PREP 0x65u     /* OUT: wValue variant bits, motor-off CR/LF counter preparation */
#define APP_REQUEST_GPIO_SNAPSHOT 0x66u  /* IN: wValue = 64-word chunk of the GPIO alias/bank snapshot */
#define APP_REQUEST_LF_JOG 0x67u         /* OUT: wValue = raw LF command (1400..2600, 2000 neutral), wIndex = ms (1..2000) */
#define APP_REQUEST_LF_STATUS 0x68u      /* IN: last LF jog result */
#define APP_REQUEST_PAPER_FEED 0x69u     /* OUT: wValue = mode (0 feed+eject, 1 feed+hold, 2 eject), wIndex = advance counts/64 (0 default) */
#define APP_REQUEST_PAPER_STATUS 0x6au   /* IN: 12 words, see src/paper_feed.c */
#define APP_REQUEST_PAPER_MANUAL 0x6bu   /* OUT: wValue 0 stop, 1 reverse (feeder), 2 forward; 10 s auto-stop */
#define APP_REQUEST_CR_HOME 0x6cu        /* OUT: home the print carriage against the park wall */
#define APP_REQUEST_CR_MOVE 0x6du        /* OUT: wValue = target counts / 64 from the park wall (left positive), wIndex = cruise counts/s / 100 (0 default) */
#define APP_REQUEST_CR_STATUS 0x6eu      /* IN: 12 words, see src/carriage.c */
#define APP_REQUEST_CR_TUNE 0x6fu        /* OUT: wValue = parameter id, wIndex = value (bench tuning) */
#define APP_REQUEST_PAGE_DRY 0x71u       /* OUT: wValue = bands | cruise/1000 << 8, wIndex = band advance counts/64 (0 default); dry-run page loop */
#define APP_REQUEST_PAGE_DRY_STATUS 0x72u /* IN: 10 words, see src/page_job.c */
#define APP_REQUEST_HEAD_ADC 0x80u       /* IN: head thermistor ADC means and samples (read-only) */
#define APP_REQUEST_BTN 0x70u            /* control-OUT, no data: enable front-panel key probe (logs PANEL+0x28 on change) */
#define APP_REQUEST_MOTOR_JOG 0x73u     /* control-OUT, wValue=dir(0/1); fixed tiny pulse */
#define APP_REQUEST_HOME 0x74u          /* control-OUT, no data: bounded mechanical home */
#define APP_REQUEST_HOME_STATUS 0x75u   /* IN: 8-word prefix, optional 4-word clock extension */
#define APP_REQUEST_MOTOR_STOP 0x76u    /* control-OUT: immediate cancel */
#define APP_REQUEST_PAGE_START 0x77u    /* OUT: wValue rows, wIndex encoder pitch */
#define APP_REQUEST_PAGE_STATUS 0x78u   /* IN: 16 little-endian words */
#define APP_REQUEST_PAGE_DATA 0x79u     /* IN: wValue chunk, wIndex row */
#define APP_REQUEST_PAGE_ACK 0x7au      /* OUT: wValue row; advance after full read */

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
void scanner_burst_request(unsigned n, unsigned exposure_eighths, unsigned gain, unsigned skip_events,
                           unsigned afe_offset);
unsigned scanner_burst_dump(uint8_t *out, unsigned chunk, unsigned max);
bool scan_page_park(uint32_t offset);
/* Continuous moving line acquisition (scanner.c). */
unsigned scanner_line_max_rows(void);
bool scanner_line_prepare(unsigned rows, unsigned pitch, unsigned period);
bool scanner_line_start(uint32_t first, uint32_t lead, uint32_t budget_ms);
void scanner_line_poll(void);
void scanner_line_stop(void);
unsigned scanner_line_ready(void);
bool scanner_line_done(void);
bool scanner_line_failed(void);
const uint16_t *scanner_line_row(unsigned row);
unsigned scanner_line_dump(uint8_t *out, uint32_t chunk, unsigned max);
bool scanner_line_probe_config(unsigned lines, unsigned drain);   /* experiment: stationary probe lines */
#define APP_REQUEST_LINE_DUMP 0x5du      /* IN: chunk = wIndex << 16 | wValue of the raw line buffer */
#define APP_REQUEST_LINE_PROBE 0x7du     /* OUT: wValue = probe lines (0..4), wIndex = drain events (0..32) */
#define APP_REQUEST_HOME_SPEED 0x7eu     /* OUT: wValue = fast-return cruise / 100 (0 = classic homing) */
#define APP_REQUEST_LINE_AFE 0x7fu       /* OUT: wValue = exposure eighths (1..8), wIndex = AFE gain (1..13) */
void print_selftest(void);          /* decode an embedded BJL band, emit result to the debug log */
void print_bulk_begin(unsigned total); /* start a USB bulk print transfer of `total` bytes (USB 0x61) */
void print_bulk_feed(const uint8_t *data, unsigned len); /* feed received bulk bytes; decodes when complete */
bool print_is_active(void);         /* true while a bulk print transfer is in progress (gates bulk OUT arming) */
/* Cartridge fuse-ROM identification (src/cartridge.c): read-only, bounded. */
bool cartridge_request(unsigned heads, unsigned wait_units);
bool cartridge_busy(void);
void cartridge_poll(void);
unsigned cartridge_status(uint8_t *out);
unsigned head_adc_status(uint8_t *out);   /* 16-sample head thermistor read per head (USB 0x80) */
uint32_t head_adc_read(unsigned head);    /* one 16-sample mean: 0 black, 1 colour */
/* Shared head interface (src/cartridge.c): stock block init, prepare, acquire, release, shutdown. */
void head_block_init(void);
bool head_if_prepared(void);
void head_if_prepare(void);
void head_if_acquire(void);
void head_if_release(void);
void head_if_shutdown(void);
bool cartridge_ranks(unsigned *bk, unsigned *cl1, unsigned *cl2);
/* Stationary prefire / spit on the black lanes (src/head_fire.c). */
#define APP_REQUEST_HEAD_FIRE 0x81u      /* OUT: wValue = count (0 = 100) | options (0x100 dry run, 0x200 keep prepared, 0x400 record 1 only, 0x800 record 2 only, 0x1000 colour list ID 0x25 with count in sixths), wIndex = ambient half-degrees C (0 = 50) */
#define APP_REQUEST_HEAD_FIRE_STATUS 0x82u /* IN: 20 words, see src/head_fire.c */
#define APP_REQUEST_HEAD_WORD_A 0x83u    /* OUT: wValue | wIndex << 16 = record word for 0xc0c291e0 (experiment, used with 0x81 option 0x2000) */
#define APP_REQUEST_HEAD_WORD_B 0x84u    /* OUT: same for 0xc0c291e8 */
bool head_fire_set_word(unsigned which, uint32_t value);
#define APP_REQUEST_HEAD_PATTERN_A 0x89u  /* OUT: wValue | wIndex << 16 = pattern word for 0xc0c29230 (0 = stock) */
#define APP_REQUEST_HEAD_PATTERN_B 0x8au  /* OUT: same for 0xc0c29234 */
bool head_fire_set_pattern(unsigned which, uint32_t value);
/* Stamp job: fire entries at carriage positions while crawling (src/head_fire.c). */
#define APP_REQUEST_STAMP_COMMIT 0x85u   /* OUT: wValue = entry index, wIndex = x / 64; copies the 0x83/0x84 words */
#define APP_REQUEST_STAMP_CLEAR 0x86u    /* OUT: forget all entries */
#define APP_REQUEST_STAMP_RUN 0x87u      /* OUT: wValue = count (0 = 15) | options (0x100 dry, 0x200 keep), wIndex = crawl counts/s / 100 */
#define APP_REQUEST_STAMP_STATUS 0x88u   /* IN: 12 words */
bool stamp_commit(unsigned index, uint32_t x);
bool stamp_clear(void);
bool stamp_run(unsigned value, unsigned crawl_units);
bool stamp_busy(void);
void stamp_cancel(void);
unsigned stamp_status(uint8_t *out);
/* Register tunnel for hardware-in-the-loop emulation (src/tunnel.c). */
#define APP_REQUEST_TUNNEL_ADDR 0x90u    /* OUT: wValue | wIndex << 16 = address */
#define APP_REQUEST_TUNNEL_READ 0x91u    /* IN: up to 256 bytes from the address (allowlisted) */
#define APP_REQUEST_TUNNEL_WRITE 0x92u   /* OUT: wValue | wIndex << 16 = 32-bit value (unlocked, allowlisted) */
#define APP_REQUEST_TUNNEL_UNLOCK 0x93u  /* OUT: wValue = key enables writes, anything else locks */
#define APP_REQUEST_TUNNEL_STATUS 0x94u  /* IN: 7 words */
bool tunnel_set_address(uint32_t a);
unsigned tunnel_read(uint8_t *out, unsigned max);
bool tunnel_write(uint32_t value);
bool tunnel_unlock(unsigned key);
unsigned tunnel_status(uint8_t *out);
bool head_fire_request(unsigned value, unsigned ambient);
void head_fire_poll(void);
bool head_fire_busy(void);
unsigned head_fire_status(uint8_t *out);
unsigned print_mech_dump(uint8_t *out);  /* read-only CR/LF register dump (USB 0x64) */
bool print_mech_prepare(unsigned variant); /* motor-off counter preparation (USB 0x65) */
unsigned print_mech_snapshot(uint8_t *out, unsigned chunk); /* read-only GPIO snapshot (USB 0x66) */
bool print_mech_lf_request(unsigned command, unsigned ms);  /* bounded LF pulse (USB 0x67) */
bool print_mech_lf_busy(void);
void print_mech_poll(void);
unsigned print_mech_lf_status(uint8_t *out);                /* USB 0x68 */
bool print_mech_lf_button(unsigned dir);                    /* panel: bounded carriage pulse (bench) */
/* Closed-loop paper feed (src/paper_feed.c): pickup with bounded retries, advance, eject. */
bool paper_feed_request(unsigned mode, unsigned advance_units);
void paper_feed_poll(void);
void paper_feed_cancel(void);
bool paper_feed_busy(void);
bool paper_feed_holding(void);
unsigned paper_feed_status(uint8_t *out);
/* Print carriage position control (src/carriage.c). */
bool carriage_home_request(void);
bool carriage_move_request(int32_t x_target, uint32_t cruise_speed);
void carriage_poll(void);
void carriage_cancel(void);
bool carriage_busy(void);
bool carriage_referenced(void);
int32_t carriage_position(void);
unsigned carriage_status(uint8_t *out);
bool carriage_tune(unsigned id, unsigned value);
unsigned paper_feed_result(void);
/* Dry-run page job (src/page_job.c). */
bool page_job_request(unsigned bands, uint32_t advance, uint32_t left, uint32_t right, uint32_t speed);
void page_job_poll(void);
void page_job_cancel(void);
bool page_job_busy(void);
unsigned page_job_status(uint8_t *out);
bool paper_manual_request(unsigned dir);   /* bench: continuous LF run (USB 0x6b) */
bool paper_manual_toggle(unsigned dir);
bool paper_manual_running(void);
void paper_manual_poll(void);
bool scanner_motor_shared_init(void);     /* scanner driver's motor-off shared setup, for experiments */
void buttons_request(void);         /* enable the front-panel key probe (USB 0x70) */
#define APP_REQUEST_BTN_MAP 0x8bu        /* OUT: wValue = button map (0 default, 1 feed bench), see src/buttons.c */
bool buttons_select_map(unsigned map);
void buttons_poll(void);            /* always-active keys, optional change log */
void scanner_poll(void);            /* run an armed action, if any */
void scanner_lamp_toggle(void);     /* toggle continuous CIS illumination (Cancel key) */
bool scanner_motor_request(unsigned dir);  /* queue fixed pulse, reject overlap/cooldown */
bool scanner_home_request(void);
unsigned scanner_home_status(uint8_t *out);
uint32_t app_clock_ticks(void); /* extends low-29-bit hardware counter deltas */
bool scanner_motor_busy(void);
void scanner_motor_poll(void);             /* drive deadline checked every main loop */
/* Service the 1 ms motor controller from inside long work. It stops a move
 * when more than 4 ms pass between polls (~15 k instructions on this CPU), so
 * protocol and encoding loops call this every few thousand instructions. */
void app_yield(void);
extern const char *app_section;   /* protocol work in progress, for motor diagnostics */
void scanner_motor_cancel(void);
void scanner_motor_shutdown(void);         /* stop and inhibit until reset */
unsigned scanner_home_result(void);
uint32_t scanner_motor_position(void);
uint32_t scanner_motor_reference(void);
bool scanner_motor_page_move(uint32_t position);
bool scanner_motor_scan_move(uint32_t endpoint, uint32_t speed);  /* continuous scan travel */
bool scanner_motor_at_reference(void);  /* idle, referenced and parked at home */
bool scanner_motor_set_return_speed(unsigned counts_per_s); /* experiment: fast return cruise, 0 or 20000..120000 */
bool scanner_line_set_afe(unsigned exposure_eighths, unsigned gain); /* experiment: continuous-scan optics */
bool scan_page_continuous(void);
void scan_page_set_speed(unsigned counts_per_s);   /* diagnostic: continuous travel speed */
#define APP_REQUEST_SCAN_SPEED 0x7cu     /* OUT: wValue = continuous speed / 100 (130..300) */
bool scanner_motor_move_ok(void);
bool scanner_capture_row(bool first); /* bounded stationary capture; motor must be off */
void scanner_lamp_off(void);
bool scan_page_request(unsigned rows, unsigned pitch);
bool scan_page_start(unsigned rows, unsigned pitch, uint32_t start, bool owned);
const uint16_t *scan_page_take(unsigned *row);  /* owned page: ready row samples, else NULL */
bool scan_page_release(unsigned row);           /* owned page: row consumed, move on */
bool scan_page_failed(void);
const uint16_t *scanner_row(void);              /* last captured row: 2592 LE samples */
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
