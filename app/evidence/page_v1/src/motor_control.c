/* Source transcription of stock f76ce (smooth acceleration), f8682
 * (speed PI) and f8772..f87ce (anti-windup/clamp/command reflection).
 * Home selection 1: 400->28000/50 ticks, Ki=500/10, u=3500..5000.
 * Backoff selection 25: 1200->20000/70 ticks, Ki=1000/10, u=0..5000.
 * Both use Kp=315/1000 and Kd=0/100 at startup. Each tick is 1ms.
 * No stock executable code or compiler-runtime binary is linked.
 */
#include "motor_control.h"

uint32_t motor_divide(uint64_t n, uint32_t d)
{
    if (!d || (n >> 32) >= d) return UINT32_MAX;
    uint64_t rem = n >> 32;
    uint32_t low = (uint32_t)n, q = 0;
    for (unsigned i = 0; i < 32; ++i) {
        rem = (rem << 1) | (low >> 31); low <<= 1; q <<= 1;
        if (rem >= d) { rem -= d; q |= 1u; }
    }
    return q;
}

void motor_control_reset(struct motor_control *s)
{
    s->integral = s->correction = 0; s->command = 5000; s->windup = 0;
}

uint32_t motor_control_ramp(uint32_t tick, unsigned backoff)
{
    uint32_t n = backoff ? 70u : 50u;
    uint32_t lo = backoff ? 1200u : 400u, hi = backoff ? 20000u : 28000u;
    if (tick >= n) return hi;
    uint64_t v = (uint64_t)(3u*n - 2u*tick) * (hi-lo) * tick * tick;
    /* For nonnegative values, three successive /n equal one /(n*n*n). */
    return lo + motor_divide(v, n*n*n);
}

uint32_t motor_control_step(struct motor_control *s, int32_t wanted,
                            int32_t measured, unsigned backoff, unsigned dir)
{
    int32_t error = wanted - measured;
    int32_t di = error * (backoff ? 100 : 50);
    if (di > 0 ? !(s->windup & 1u) : !(s->windup & 2u)) {
        /* Explicit wrap matches ARM arithmetic without signed C overflow. */
        s->integral = (int32_t)((uint32_t)s->integral + (uint32_t)di);
    }
    s->correction = (error + s->integral / 1000) * 315 / 1000;
    int16_t raw = (int16_t)(5000 - s->correction);
    if (s->command < raw) s->windup &= (uint8_t)~1u;
    else s->windup &= (uint8_t)~2u;
    int16_t lower = backoff ? 0 : 3500;
    s->command = raw;
    if (raw > 5000) { s->command = 5000; s->windup |= 2u; }
    else if (raw < lower) { s->command = lower; s->windup |= 1u; }
    int32_t reflected = dir ? s->command : 10000 - s->command;
    return (uint32_t)(2 * reflected / 5);
}
