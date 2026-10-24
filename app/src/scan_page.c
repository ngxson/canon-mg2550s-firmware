/* Stop-and-capture page PoC. Each line remains immutable until the host has
 * read all chunks and acknowledged it. No buffering assumption can lose a row.
 * Geometry uses nominal stock counts/inch; optical origin is not calibrated. */
#include "app.h"

enum page_phase { PAGE_IDLE, PAGE_HOME, PAGE_SETTLE, PAGE_CAPTURE, PAGE_READY,
                  PAGE_STEP, PAGE_RETURN, PAGE_DONE, PAGE_FAILED, PAGE_CANCELLED };
enum page_error { PAGE_OK, ERR_HOME, ERR_STEP, ERR_CAPTURE, ERR_HOST_TIMEOUT,
                  ERR_POSITION, ERR_DEADLINE, ERR_CANCEL };
static enum page_phase page_phase;
static enum page_error page_error;
static unsigned row, rows, pitch, last_acked;
static uint32_t origin, capture_start, capture_end, chunks_read;
static uint32_t started_ms, phase_ms, contact_ms;
static bool action_started;
#define ROW_SAMPLES 2592u
#define ROW_BYTES (ROW_SAMPLES * 2u)
#define ROW_CHUNKS ((ROW_BYTES + 255u) / 256u)

bool scan_page_busy(void) { return page_phase >= PAGE_HOME && page_phase <= PAGE_RETURN; }

static void transition(enum page_phase next)
{
    page_phase = next; phase_ms = time_ms(); action_started = false;
}

static void end_page(enum page_phase result, enum page_error error)
{
    scanner_motor_cancel(); scanner_lamp_off();
    page_error = error; transition(result);
    dbg_puts("[page] end state="); dbg_dec(result);
    dbg_puts(" error="); dbg_dec(error); dbg_puts(" rows="); dbg_dec(row); dbg_putc('\n');
}

void scan_page_cancel(void)
{
    if (scan_page_busy()) end_page(PAGE_CANCELLED, ERR_CANCEL);
}

bool scan_page_request(unsigned count, unsigned spacing)
{
    if (!count || count > 880u || spacing < 256u || spacing > 1024u ||
        (count - 1u) * spacing > 450048u || scan_page_busy() ||
        scanner_motor_busy() || print_is_active()) return false;
    rows = count; pitch = spacing; row = 0; last_acked = UINT32_MAX;
    origin = capture_start = capture_end = chunks_read = 0;
    page_error = PAGE_OK; started_ms = time_ms();
    transition(PAGE_HOME);
    dbg_puts("[page] start rows="); dbg_dec(rows); dbg_puts(" pitch="); dbg_dec(pitch); dbg_putc('\n');
    return true;
}

unsigned scan_page_status(uint8_t *out)
{
    uint32_t data[16] = {0x4750534du, 1u, page_phase, page_error,
        row, rows, ROW_SAMPLES, ROW_BYTES, pitch, origin,
        scanner_motor_position(), capture_start, capture_end, chunks_read,
        time_ms() - started_ms, 0x46u};
    memcpy(out, data, sizeof(data)); return sizeof(data);
}

unsigned scan_page_data(uint8_t *out, unsigned requested_row, unsigned chunk, unsigned max)
{
    if (page_phase != PAGE_READY || requested_row != row || chunk >= ROW_CHUNKS) return 0;
    if (max > 256u) max = 256u;
    unsigned remaining = ROW_BYTES - chunk * 256u;
    unsigned expected = remaining < 256u ? remaining : 256u;
    if (max > expected) max = expected;
    unsigned n = scanner_dump(out, chunk, max);
    if (n == expected) chunks_read |= 1u << chunk;
    contact_ms = time_ms(); return n;
}

bool scan_page_ack(unsigned requested_row)
{
    /* Idempotent retry after an uncertain status-stage acknowledgement. */
    if (requested_row == last_acked) return true;
    if (page_phase != PAGE_READY || requested_row != row ||
        chunks_read != ((1u << ROW_CHUNKS) - 1u)) return false;
    last_acked = row++;
    transition(row == rows ? PAGE_RETURN : PAGE_STEP);
    return true;
}

void scan_page_poll(void)
{
    if (!scan_page_busy()) return;
    uint32_t now = time_ms();
    if (now - started_ms > 900000u) { end_page(PAGE_FAILED, ERR_DEADLINE); return; }
    if (page_phase == PAGE_HOME || page_phase == PAGE_RETURN) {
        if (now - phase_ms > 30000u) { end_page(PAGE_FAILED, ERR_HOME); return; }
        if (!action_started) {
            scanner_lamp_off();
            if (scanner_home_request()) action_started = true;
            return;
        }
        if (scanner_motor_busy()) return;
        if (scanner_home_result() != 2u) { end_page(PAGE_FAILED, ERR_HOME); return; }
        if (page_phase == PAGE_RETURN) { end_page(PAGE_DONE, PAGE_OK); return; }
        origin = scanner_motor_reference(); transition(PAGE_SETTLE);
    } else if (page_phase == PAGE_STEP) {
        if (!action_started) {
            if (!scanner_motor_page_move(origin + row * pitch)) {
                end_page(PAGE_FAILED, ERR_STEP); return;
            }
            action_started = true;
        } else if (!scanner_motor_busy()) {
            if (!scanner_motor_move_ok()) { end_page(PAGE_FAILED, ERR_STEP); return; }
            transition(PAGE_SETTLE);
        }
    } else if (page_phase == PAGE_SETTLE) {
        if (now - phase_ms < 30u) return;
        int32_t error = (int32_t)(scanner_motor_position() - (origin + row * pitch));
        if (error < -48 || error > 48) { end_page(PAGE_FAILED, ERR_POSITION); return; }
        transition(PAGE_CAPTURE);
    } else if (page_phase == PAGE_CAPTURE) {
        /* Existing proven capture blocks for ~110ms, always with drive OFF.
         * USB and Cancel resume afterward; no active servo tick is delayed. */
        capture_start = scanner_motor_position();
        bool ok = scanner_capture_row();
        capture_end = scanner_motor_position();
        int32_t drift = (int32_t)(capture_end - capture_start);
        if (!ok) { end_page(PAGE_FAILED, ERR_CAPTURE); return; }
        if (drift < -8 || drift > 8) { end_page(PAGE_FAILED, ERR_POSITION); return; }
        chunks_read = 0; transition(PAGE_READY); contact_ms = time_ms();
        if (!(row & 31u)) { dbg_puts("[page] row ready="); dbg_dec(row); dbg_putc('\n'); }
    } else if (page_phase == PAGE_READY && now - contact_ms > 15000u) {
        end_page(PAGE_FAILED, ERR_HOST_TIMEOUT);
    }
}
