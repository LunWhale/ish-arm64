#ifndef NATIVE_OFFLOAD_H
#define NATIVE_OFFLOAD_H

#include <stdbool.h>
#include <sys/types.h>
#include <signal.h>
struct task;

// ============================================================================
// Native Offload — bypass emulation for selected guest binaries
// ============================================================================
//
// When guest execve() matches a registered binary name, execution is
// intercepted and routed to either:
//   1. An in-process handler function (iOS + macOS)
//   2. A native host binary via posix_spawn (macOS only)
//
// The handler receives argc/argv (with guest paths translated to host paths)
// and pipe-backed stdio fds. Output written to stdout_fd/stderr_fd is
// forwarded through the guest TTY driver, so it appears in the terminal UI.
//
// --- Quick integration guide ---
//
//   // 1. Define your handler (runs on guest thread, in-process)
//   static int my_tool_main(int argc, char **argv,
//                           int stdin_fd, int stdout_fd, int stderr_fd) {
//       dprintf(stdout_fd, "hello from native handler\n");
//       return 0; // exit code
//   }
//
//   // 2. Register it (call once at app startup, before guest runs)
//   native_offload_add_handler("mytool", my_tool_main);
//
//   // 3. Done — when guest runs `execve("/usr/bin/mytool", ...)`,
//   //    my_tool_main() is called instead of emulating the ELF binary.
//
// Notes for handler authors:
//   - argv paths starting with '/' are auto-translated to host filesystem
//   - CWD is set to the host equivalent of the guest's CWD
//   - Use dprintf(stdout_fd, ...) and dprintf(stderr_fd, ...) for output
//   - stdin_fd may be -1 if guest stdin is not backed by a real host fd
//   - Return 0 for success, non-zero for failure (becomes guest exit code)
//   - Handler runs on the guest thread; do_exit() is called after return
//
// macOS CLI also supports host binary offload:
//   ish -n ffmpeg                     # auto-detect /opt/homebrew/bin/ffmpeg
//   ish -n ffprobe=/usr/local/bin/ffprobe  # explicit path
//
// ============================================================================

#define NATIVE_OFFLOAD_MAX 32

// Handler function signature. Called in-process on the guest thread.
// Receives translated argv (guest absolute paths → host paths) and
// pipe-backed stdio fds for output.
// Must return an exit code (0 = success).
typedef int (*native_handler_func)(int argc, char **argv,
                                   int stdin_fd, int stdout_fd, int stderr_fd);

// Register an in-process handler for a guest binary name.
// When guest execve() basename matches guest_name, handler is called
// instead of emulating the binary. Takes priority over host binary lookup.
// Returns 0 on success, -1 if registry is full.
int native_offload_add_handler(const char *guest_name, native_handler_func handler);

// Register a host binary offload (macOS CLI only, uses posix_spawn).
// spec is "name" or "name=/host/path". Returns 0 on success.
int native_offload_add(const char *spec);

// Check if a guest binary should be offloaded.
// Returns native host path, "[builtin]" for handler-only, or NULL.
const char *native_offload_lookup(const char *guest_path);

// Execute the offloaded binary (handler or posix_spawn).
// Takes over the current guest task and calls do_exit(). Does not return
// on success. Returns negative errno on failure.
int native_offload_exec(const char *native_path,
                        const char *guest_file,
                        size_t argc, const char *argv,
                        const char *envp);

// Forward a signal to the native process backing a proxy task.
// Returns true if the signal was forwarded.
bool native_offload_forward_signal(struct task *task, int sig);

// ============================================================================
// Symbol-level native offload (ARM64 guest only)
// ============================================================================
//
// The API above offloads at *binary* granularity: a whole guest command
// (matched by execve) runs as a host process or in-process main(). The API
// below offloads at *function* granularity: a single guest function (matched
// by its entry PC) runs as a native handler, skipping gadget translation for
// that call. Same "native offload" concept, finer grain.
//
// Use binary-level for "run this whole tool natively" (curl, ffmpeg).
// Use symbol-level for "run this hot function natively" (memcpy, Py compile).
//
//   // 1. Define a handler that reads guest args and writes results.
//   static enum nsym_result my_memcpy(struct nsym_ctx *c, void *user) {
//       addr_t dst = nsym_arg(c, 0), src = nsym_arg(c, 1);
//       uint64_t n = nsym_arg(c, 2);
//       // ... value-copy via nsym_read/nsym_write ...
//       nsym_set_ret(c, dst);
//       return NSYM_HANDLED;   // or NSYM_DECLINED to fall back to guest code
//   }
//   // 2. Register (binary basename + symbol name).
//   native_offload_add_symbol("libc.musl-aarch64.so.1", "memcpy",
//                             my_memcpy, NULL);
//
// Lossless: if disabled (ISH_OFFLOAD unset), unmatched, or a handler returns
// NSYM_DECLINED, the guest executes its own code unchanged.

#include <stdint.h>
#include "misc.h"   /* addr_t */
struct tlb;
struct cpu_state;

enum nsym_result {
    NSYM_HANDLED,    /* handler did the work; resume guest per ctx->resume */
    NSYM_DECLINED,   /* handler opted out; run the guest function normally */
};

enum nsym_resume {
    NSYM_RESUME_RET, /* return to link register (x30) — normal call return */
    NSYM_RESUME_PC,  /* jump to ctx->resume_pc (handler set it explicitly) */
};

// Context for a symbol-level handler: wraps guest CPU + TLB. All memory
// access is value-copy (fork/CoW safe), matching the mem* bypass discipline.
struct nsym_ctx {
    struct cpu_state *cpu;
    struct tlb *tlb;
    enum nsym_resume resume;   /* handler sets this; default RET */
    addr_t resume_pc;          /* used when resume == NSYM_RESUME_PC */
};

// AArch64 AAPCS64: integer args in x0..x7, return in x0.
uint64_t nsym_arg(struct nsym_ctx *ctx, int n);
void     nsym_set_ret(struct nsym_ctx *ctx, uint64_t v);
// Guest memory access — value-copy, fork-safe. Return false on fault.
bool nsym_read(struct nsym_ctx *ctx, addr_t gaddr, void *buf, size_t len);
bool nsym_write(struct nsym_ctx *ctx, addr_t gaddr, const void *buf, size_t len);

typedef enum nsym_result (*nsym_handler_func)(struct nsym_ctx *ctx, void *user);

// Register a symbol-level offload: (binary basename, symbol) -> handler.
// addr_hint is a fixed guest entry address for the no-ASLR case (0 = resolve
// by name later via native_offload_bind_symbol). Returns id >=0 or -1.
int native_offload_add_symbol(const char *binary, const char *symbol,
                              nsym_handler_func handler, void *user);

// Bind a registered symbol to a runtime entry address (called by the loader
// when a matching binary maps a matching symbol). Idempotent.
void native_offload_bind_symbol(const char *binary, const char *symbol, addr_t addr);

// Dispatch hook — called from the ARM64 JIT loop with the current guest PC.
// Returns true if a handler fired (guest state advanced), false to run guest
// code. Cheap on the common miss (a few address compares).
bool native_offload_sym_dispatch(addr_t pc, struct cpu_state *cpu, struct tlb *tlb);

// True when at least one symbol target is registered. The dispatch loop
// checks this to skip the scan entirely when nothing is registered — offload
// is lossless, so "registered == active" is the only gate (no env switch,
// matching binary-level offload where registration alone enables it).
bool native_offload_sym_active(void);

// Register built-in symbol targets (mem* for verification; Python compile
// when built with -DISH_OFFLOAD_PYCOMPILE). Called once from _sym_active().
// A no-op unless the relevant env/build flags are set.
void native_offload_sym_init_builtins(void);

// Register a symbol target with a fixed guest entry address (no-ASLR case),
// pre-binding it immediately instead of waiting for loader resolution.
// Prefer native_offload_add_symbol + native_offload_bind_symbol when symbol
// resolution is available.
int native_offload_add_symbol_hinted(const char *binary, const char *symbol,
                                     addr_t addr_hint, nsym_handler_func handler,
                                     void *user);

#endif
