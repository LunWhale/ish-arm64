/*
 * native_offload_cached.c — cached-gadget offload (see native_offload.h).
 *
 * Third offload granularity. A registered guest function's whole block is
 * replaced, at block-compile time, by a single "cached gadget" that calls a
 * native spec_fn reproducing the function's logic in guest semantics. Unlike
 * symbol-level offload it never crosses the host/guest object boundary — spec_fn
 * only reads/writes cpu_state regs and guest memory (via the TLB), so it works
 * for any function (including ones returning guest PyObjects).
 *
 * spec_fn is generated offline (clang auto-translation of the guest function's
 * disassembly) and compiled statically into iSH; native_offload_cached_init
 * registers the shipped targets. No spec_fn is hardcoded in this core file.
 *
 * ARM64 guest only.
 */
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include "kernel/native_offload.h"
#include "emu/tlb.h"
#include "emu/arch/arm64/cpu.h"

#define CACHED_MAX 32

struct cached_target {
    const char *binary;
    const char *symbol;
    addr_t guest_addr;      /* function entry; block starting here is replaced */
    cached_spec_fn spec_fn;
};

static struct cached_target cached_table[CACHED_MAX];
static int cached_count = 0;
static uint64_t cached_hits[CACHED_MAX];

int native_offload_add_cached(const char *binary, const char *symbol,
                              addr_t guest_addr, cached_spec_fn spec_fn) {
    if (cached_count >= CACHED_MAX) return -1;
    int id = cached_count++;
    cached_table[id] = (struct cached_target){
        .binary = binary, .symbol = symbol,
        .guest_addr = guest_addr, .spec_fn = spec_fn,
    };
    return id;
}

cached_spec_fn native_offload_cached_lookup(addr_t pc) {
    for (int i = 0; i < cached_count; i++) {
        if (cached_table[i].guest_addr == pc) {
            if (cached_hits[i]++ == 0 && getenv("ISH_OFFLOAD_STATS"))
                fprintf(stderr, "[offload:cached] first hit: %s:%s @ %llx\n",
                        cached_table[i].binary ? cached_table[i].binary : "?",
                        cached_table[i].symbol, (unsigned long long)pc);
            return cached_table[i].spec_fn;
        }
    }
    return NULL;
}

bool native_offload_cached_active(void) {
    static bool inited = false;
    if (!inited) { inited = true; native_offload_cached_init(); }
    return cached_count > 0;
}

void native_offload_cached_init(void) {
    /* Register shipped cached-gadget targets here (generated spec_fns declared
     * extern from the offline tool's output). None yet. Test handlers self-
     * register via a constructor in kernel/offload_tests/ (no hook needed). */
}
