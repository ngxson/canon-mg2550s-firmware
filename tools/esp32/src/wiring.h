/* Connection-local wiring proof. No persistent pin selection. */
#ifndef BRIDGE_WIRING_H
#define BRIDGE_WIRING_H
#include <stdbool.h>
#include <stdint.h>
#include "protocol.h"

#define WIRING_PROBE_US 3000000LL
enum wiring_phase { WIRING_UNKNOWN, WIRING_PROBING, WIRING_VERIFIED, WIRING_FAILED };
enum wiring_proof { WIRING_NO_PROOF, WIRING_REJECT90, WIRING_HEADER_READ };
enum wiring_event { WIRING_BAD, WIRING_HEADER, WIRING_POINTER, WIRING_READ, WIRING_RESULT };
struct wiring {
    enum wiring_phase phase;
    enum wiring_proof proof;
    unsigned orientation, exchanges;
    bool awaiting_read;
    int64_t deadline;
};
void wiring_start(struct wiring *w, int64_t now);
/* True means attach the second orientation, after releasing both pins. */
bool wiring_expire(struct wiring *w, int64_t now);
void wiring_observe(struct wiring *w, enum wiring_event event, uint32_t value);
enum wiring_event wiring_decode(const uint8_t *bytes, unsigned length);
void wiring_probe_reply(uint8_t reply[FRAME_BYTES]);
#endif
