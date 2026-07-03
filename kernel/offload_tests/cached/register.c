/*
 * Cached-gadget offload test registration (dev only).
 *
 * Compiled only with -Doffload_test_cached=true. Self-registers the auto-
 * translated spec_fns as cached-gadget targets via a constructor at startup —
 * the product core (native_offload_cached.c) has NO test hook at all. When this
 * file isn't compiled, nothing is registered.
 */
#include "kernel/native_offload.h"

/* Auto-translated spec_fns (kernel/offload_tests/cached/spec_*.c). */
void spec_mix(struct cpu_state *cpu, struct tlb *tlb);

__attribute__((constructor))
static void offload_test_cached_register(void) {
    /* tests/offload/cached/mixbench.c, non-PIE, mix @ 0x400314 */
    native_offload_add_cached("mixbench", "mix", 0x400314, spec_mix);
}
