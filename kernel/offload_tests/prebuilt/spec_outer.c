/* AUTO-GENERATED prebuilt-gadget spec_fn for guest `outer` — DO NOT EDIT.
 * Source: tools/prebuilt_gadget_gen/callbench  addr 0x0000000000400338
 * Produced by tools/prebuilt_gadget_gen/gen.sh (guest asm -> equivalent C).
 * Compiled only with -Doffload_test_prebuilt=true. */
#include <stdint.h>
#include "emu/arch/arm64/cpu.h"
#include "emu/tlb.h"
#include "kernel/native_offload.h"  /* prebuilt_call for bl/blr sites */

static uint64_t ror64(uint64_t v, unsigned r) { return (v >> r) | (v << (64 - r)); }
static uint64_t g_fa, g_fb;
#define FLAG_CMP(x,y) do { g_fa=(x); g_fb=(y); } while(0)
#define FLAG_EQ (g_fa == g_fb)
#define FLAG_NE (g_fa != g_fb)
#define FLAG_GT ((int64_t)g_fa >  (int64_t)g_fb)   /* signed */
#define FLAG_LT ((int64_t)g_fa <  (int64_t)g_fb)
#define FLAG_GE ((int64_t)g_fa >= (int64_t)g_fb)
#define FLAG_LE ((int64_t)g_fa <= (int64_t)g_fb)
#define FLAG_HI (g_fa >  g_fb)                     /* unsigned */
#define FLAG_LO (g_fa <  g_fb)
#define FLAG_HS (g_fa >= g_fb)
#define FLAG_LS (g_fa <= g_fb)
#define SP (cpu->sp)                               /* stack pointer */
/* Memory ops go through the guest TLB (fork/CoW safe). 64-bit + byte. */
#define PB_LDR(dst, addr) do { uint64_t _v=0; tlb_read(tlb,(addr),&_v,8); (dst)=_v; } while(0)
#define PB_STR(addr, val) do { uint64_t _v=(val); tlb_write(tlb,(addr),&_v,8); } while(0)
#define PB_LDRB(dst, addr) do { uint8_t _b=0; tlb_read(tlb,(addr),&_b,1); (dst)=_b; } while(0)

void spec_outer(struct cpu_state *cpu, struct tlb *tlb) {
    (void)tlb;
    if ((cpu->regs[1])==0) goto L_400374;
    SP += -32; PB_STR(SP, cpu->regs[29]); PB_STR(SP + 8, cpu->regs[30]);
    cpu->regs[29] = SP;
    PB_STR((SP + 16), cpu->regs[19]); PB_STR((SP + 16) + 8, cpu->regs[20]);
    cpu->regs[20] = cpu->regs[1];
    cpu->regs[19] = 0ULL;
L_400350:
    cpu->regs[0] = cpu->regs[0] ^ cpu->regs[19];
    cpu->regs[30] = 0x400358ULL; prebuilt_call(cpu, tlb, 0x40031cULL);
    cpu->regs[0] = cpu->regs[0] ^ (cpu->regs[0] >> 31);
    cpu->regs[19] = cpu->regs[19] + 1ULL;
    FLAG_CMP(cpu->regs[20], cpu->regs[19]);
    if (FLAG_NE) goto L_400350;
    PB_LDR(cpu->regs[19], (SP + 16)); PB_LDR(cpu->regs[20], (SP + 16) + 8);
    PB_LDR(cpu->regs[29], SP); PB_LDR(cpu->regs[30], SP + 8); SP += 32;
    return;
L_400374:
    return;
}
