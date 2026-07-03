#!/bin/bash
# Cached-gadget offload test: verify the auto-translated spec_mix (compiled with
# -Doffload_test_cached=true) produces bit-identical results to normal threaded
# execution, and is faster. Self-contained: rebuilds the guest binary via Docker.
#
# Usage:  tests/offload/cached/run.sh [iters]
# Requires: Docker (aarch64), a built base ish, and a build dir.
set -u
cd "$(dirname "$0")/../../.."          # repo root
ITERS="${1:-5000000}"
ROOTFS="alpine-arm64"
BUILD="build-arm64-release"
HERE="tests/offload/cached"
PASS=0; FAIL=0
say() { printf '%s\n' "$*"; }
ok()  { PASS=$((PASS+1)); say "  PASS: $*"; }
bad() { FAIL=$((FAIL+1)); say "  FAIL: $*"; }

# 1. Rebuild guest mixbench (non-PIE, fixed mix @ 0x400314) via Docker.
say "== building guest mixbench (Docker aarch64) =="
docker run --rm --platform linux/arm64 -v "$PWD/$HERE:/t" alpine:latest sh -c \
  'apk add -q gcc musl-dev >/dev/null 2>&1 && gcc -O1 -static -no-pie -fno-pie -o /t/mixbench /t/mixbench.c' \
  || { say "docker build failed"; exit 2; }
ADDR=$(nm "$HERE/mixbench" 2>/dev/null | awk '/ T mix$/{print "0x"$1}')
say "  mix @ $ADDR (register.c expects 0x400314)"
[ "$ADDR" = "0x0000000000400314" ] || bad "mix address drifted ($ADDR); update register.c + spec_mix"
cp "$HERE/mixbench" "$ROOTFS/root/mixbench"

# 2. Build the cached-test ish and a base ish.
say "== building base + cached-test ish =="
meson configure "$BUILD" -Doffload_test_cached=false >/dev/null 2>&1
ninja -C "$BUILD" ish >/dev/null 2>&1 || { say "base build failed"; exit 2; }
cp "$BUILD/ish" /tmp/ish_base_cached
meson configure "$BUILD" -Doffload_test_cached=true >/dev/null 2>&1
ninja -C "$BUILD" ish >/dev/null 2>&1 || { say "cached build failed"; exit 2; }
cp "$BUILD/ish" /tmp/ish_test_cached
meson configure "$BUILD" -Doffload_test_cached=false >/dev/null 2>&1   # restore default

# 3. Correctness: cached gadget hit + bit-identical acc.
say "== correctness =="
HIT=$(ISH_OFFLOAD_STATS=1 /tmp/ish_test_cached -r "$ROOTFS" /root/mixbench 100000 2>&1 | grep -c 'offload:cached.*mix')
[ "$HIT" -ge 1 ] && ok "cached gadget hit (mix)" || bad "cached gadget did not hit"
ACC_BASE=$(/tmp/ish_base_cached -r "$ROOTFS" /root/mixbench "$ITERS" 2>&1 | grep -oE 'acc=[0-9a-f]+')
ACC_TEST=$(/tmp/ish_test_cached -r "$ROOTFS" /root/mixbench "$ITERS" 2>&1 | grep -oE 'acc=[0-9a-f]+')
[ -n "$ACC_BASE" ] && [ "$ACC_BASE" = "$ACC_TEST" ] \
    && ok "bit-identical result ($ACC_TEST)" \
    || bad "result mismatch: base=$ACC_BASE cached=$ACC_TEST"

# 4. Performance: cached should be faster.
say "== performance =="
ms() { local t0 t1; t0=$(python3 -c 'import time;print(int(time.time()*1000))')
       "$1" -r "$ROOTFS" /root/mixbench "$ITERS" >/dev/null 2>&1
       t1=$(python3 -c 'import time;print(int(time.time()*1000))'); echo $((t1-t0)); }
B=$(ms /tmp/ish_base_cached); T=$(ms /tmp/ish_test_cached)
say "  threaded=${B}ms  cached=${T}ms"
[ "$T" -lt "$B" ] && ok "cached faster ($(python3 -c "print(f'{$B/$T:.2f}x')"))" \
                  || bad "cached not faster (threaded=$B cached=$T)"

say ""
say "== $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
