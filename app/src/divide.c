/* Unsigned division helpers required by the Arm EABI. The ARM946E-S has no
 * divide instruction and the build links without libgcc, so GCC's calls for
 * `/` and `%` on unsigned values land here (with -Os, even for constant
 * divisors). Shift-and-subtract over the quotient's bits only, using CLZ. */
#include "app.h"

unsigned long long __aeabi_uidivmod(unsigned numerator, unsigned denominator);
unsigned __aeabi_uidiv(unsigned numerator, unsigned denominator);

/* Quotient in r0 and remainder in r1: a 64-bit return value under AAPCS. */
unsigned long long __aeabi_uidivmod(unsigned numerator, unsigned denominator)
{
    unsigned quotient = 0, remainder = numerator;
    if (!denominator) return 0;
    if (denominator <= numerator) {
        int shift = __builtin_clz(denominator) - __builtin_clz(numerator);
        unsigned divisor = denominator << shift;
        for (; shift >= 0; --shift, divisor >>= 1) {
            quotient <<= 1;
            if (remainder >= divisor) { remainder -= divisor; quotient |= 1u; }
        }
    }
    return quotient | (unsigned long long)remainder << 32;
}

unsigned __aeabi_uidiv(unsigned numerator, unsigned denominator)
{ return (unsigned)__aeabi_uidivmod(numerator, denominator); }
