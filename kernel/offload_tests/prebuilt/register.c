/*
 * Pre-built gadget offload test registration (dev only).
 *
 * Compiled only with -Doffload_test_prebuilt=true. Self-registers the auto-
 * translated spec_fns as prebuilt-gadget targets via a constructor at startup —
 * the product core (native_offload_prebuilt.c) has NO test hook at all. When this
 * file isn't compiled, nothing is registered.
 */
#include "kernel/native_offload.h"

/* Auto-translated spec_fns (kernel/offload_tests/prebuilt/spec_*.c). */
void spec_mix(struct cpu_state *cpu, struct tlb *tlb);
void spec_outer(struct cpu_state *cpu, struct tlb *tlb);

__attribute__((constructor))
static void offload_test_prebuilt_register(void) {
    /* tests/offload/prebuilt/mixbench.c, non-PIE, mix @ 0x400314 */
    native_offload_add_prebuilt("mixbench", "mix", 0x400314, spec_mix);
    /* callbench.c, non-PIE, outer @ 0x400330 — mixed execution (bl inner) */
    native_offload_add_prebuilt("callbench", "outer", 0x400338, spec_outer);
}
