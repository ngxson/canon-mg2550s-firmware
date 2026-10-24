#ifndef MOTOR_CONTROL_H
#define MOTOR_CONTROL_H
#include <stdint.h>

/* Stock home-seek/backoff coefficients, f8682/f873a. No MMIO here. */
struct motor_control {
    int32_t integral, correction;
    int16_t command;
    uint8_t windup;
};
void motor_control_reset(struct motor_control *s);
uint32_t motor_control_ramp(uint32_t tick, unsigned backoff);
uint32_t motor_control_step(struct motor_control *s, int32_t wanted,
                            int32_t measured, unsigned backoff, unsigned dir);
uint32_t motor_divide(uint64_t numerator, uint32_t denominator);
#endif
