/* Versioned, optional app exception ABI. No stock-firmware dependencies. */
#ifndef MG_VECTORS_H
#define MG_VECTORS_H
#include <stdint.h>
#include <stdbool.h>

#define BOOT_VECTOR_API_ADDRESS 0x40u
#define BOOT_VECTOR_API_MAGIC 0x3158564du /* MVX1 */
#define BOOT_VECTOR_API_VERSION 1u
#define BOOT_VECTOR_STATE_ADDRESS 0xe0000880u
#define BOOT_VECTOR_COUNT 7u

enum boot_vector_index {
    BOOT_VECTOR_UNDEF, BOOT_VECTOR_SVC, BOOT_VECTOR_PREFETCH_ABORT,
    BOOT_VECTOR_DATA_ABORT, BOOT_VECTOR_RESERVED, BOOT_VECTOR_IRQ,
    BOOT_VECTOR_FIQ
};

/* Read/check magic, version and size before calling an API on an older stage.
 * install requires privileged ARM execution with IRQ/FIQ masked. Zero entries
 * retain the default fault handler. Targets must be word-aligned ARM entries
 * inside the current app payload's flash or RAM range. This checks addresses,
 * not the semantic correctness of handler instructions.
 *
 * The app owns its exception stack banks. Wrappers restore all registers,
 * original SP/LR, CPSR flags and SPSR before entering the registered handler.
 */
struct boot_vector_api {
    uint32_t magic, version, bytes, state_address;
    bool (*install)(const uint32_t targets[BOOT_VECTOR_COUNT]);
    void (*clear)(void);
};

/* Reserved internal SRAM [0xe0000880,0xe00008c4). Commit magic last. This is
 * outside stock's observed scatter-initialized [e0000000,e0000354) interval;
 * apps using this ABI must reserve it explicitly, including stack growth.
 */
struct boot_vector_state {
    uint32_t magic, magic_inverse, version;
    uint32_t target[BOOT_VECTOR_COUNT];
    uint32_t inverse[BOOT_VECTOR_COUNT];
};

bool boot_vectors_install(const uint32_t targets[BOOT_VECTOR_COUNT]);
void boot_vectors_clear(void);
uint32_t boot_vector_target(unsigned index);
uint32_t cpu_status(void);
#endif
