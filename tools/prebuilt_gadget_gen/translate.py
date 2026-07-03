#!/usr/bin/env python3
"""
最小 ARM64→C 转译器 (阶段2 PoC).
输入: objdump 反汇编; 输出: 等价 C spec_fn (读写 cpu->regs[], 访存走 tlb).
支持 hash_bytes 用到的指令子集. 遇到不支持的指令 → 报错 (安全, 不静默出错).
"""
import re, sys

def reg(r):
    """x0/w0 → regs index; wzr/xzr → -1 (零寄存器)"""
    r = r.strip().rstrip(',')
    if r in ('xzr','wzr'): return -1
    if r in ('sp','wsp'): return 32   # sp 特殊
    m = re.match(r'[wx](\d+)', r)
    return int(m.group(1)) if m else None

def R(r):
    i = reg(r)
    if i == -1: return "0ULL"
    if i == 32: return "SP"
    return f"cpu->regs[{i}]"

def is_w(r):  # 32位寄存器?
    return r.strip().rstrip(',').startswith('w')

def w32(expr):  # 截断到32位
    return f"((uint32_t)({expr}))"

def translate(lines):
    out = []
    labels = {}   # addr → C label
    body = []
    # 第一遍: 收集分支目标 (需要 label)
    insns = []
    for ln in lines:
        m = re.match(r'\s*([0-9a-f]+):\s+[0-9a-f]+\s+(\S+)\s*(.*)', ln)
        if not m: continue
        addr, op, args = int(m.group(1),16), m.group(2), m.group(3).strip()
        insns.append((addr, op, args))
        # 分支目标
        bm = re.search(r'([0-9a-f]+)\s+<', args)
        if bm and (op.startswith('b') or op=='cbz' or op=='cbnz' or op=='tbz' or op=='tbnz'):
            labels[int(bm.group(1),16)] = f"L_{bm.group(1)}"

    for addr, op, args in insns:
        if addr in labels:
            body.append(f"{labels[addr]}:")
        a = [x.strip() for x in args.split(',')] if args else []
        c = emit(addr, op, a, labels)
        if c is None:
            sys.stderr.write(f"UNSUPPORTED @ {addr:#x}: {op} {args}\n")
            return None
        body.append("    " + c if not c.endswith(":") else c)
    return body

def emit(addr, op, a, labels):
    """一条指令 → C 语句 (或 None=不支持)"""
    # 立即数
    def imm(s):
        s = s.strip().lstrip('#')
        s = s.split()[0].split('//')[0].strip()  # 去注释
        return int(s, 0)
    # AArch64 condition codes → C flag macros (defined in the generated spec).
    COND = {'eq':'FLAG_EQ','ne':'FLAG_NE','gt':'FLAG_GT','le':'FLAG_LE',
            'ge':'FLAG_GE','lt':'FLAG_LT','hi':'FLAG_HI','lo':'FLAG_LO',
            'hs':'FLAG_HS','ls':'FLAG_LS','cs':'FLAG_HS','cc':'FLAG_LO'}
    # ── mov / movz / movk ──
    if op == 'mov' and len(a)==2:
        if a[1].startswith('#'):
            return f"{R(a[0])} = {imm(a[1])}ULL;"
        return f"{R(a[0])} = {R(a[1])};"
    if op == 'movk' and len(a)>=2:
        # movk xD, #imm, lsl #sh
        sh = 0
        m = re.search(r'lsl\s*#(\d+)', args_join(a))
        val = imm(a[1])
        shm = re.search(r'#(\d+)', a[2]) if len(a)>2 else None
        if shm: sh = int(shm.group(1))
        return f"{R(a[0])} = ({R(a[0])} & ~(0xffffULL << {sh})) | ((0x{val:x}ULL & 0xffff) << {sh});"
    # ── 算术 ──
    if op in ('add','sub','eor','orr','and','mul') and len(a)==3:
        cop = {'add':'+','sub':'-','eor':'^','orr':'|','and':'&','mul':'*'}[op]
        if a[2].startswith('#'):
            rhs = f"{imm(a[2])}ULL"
        else:
            rhs = R(a[2])
        res = f"{R(a[1])} {cop} {rhs}"
        if is_w(a[0]): res = w32(res)
        return f"{R(a[0])} = {res};"
    # ── ror ──
    if op == 'ror' and len(a)==3:
        sh = imm(a[2])
        return f"{R(a[0])} = ror64({R(a[1])}, {sh});"
    # ── lsl / lsr / asr  (register or immediate shift) ──
    if op in ('lsl','lsr','asr') and len(a)==3:
        n = R(a[1])
        amt = f"{imm(a[2])}" if a[2].startswith('#') else f"({R(a[2])} & 63)"
        if op == 'lsl': res = f"{n} << {amt}"
        elif op == 'lsr': res = f"{n} >> {amt}"           # logical (unsigned)
        else: res = f"(uint64_t)((int64_t){n} >> {amt})"  # arithmetic
        if is_w(a[0]): res = w32(res)
        return f"{R(a[0])} = {res};"
    # ── csel wD, wN, wM/wzr, cond  (D = cond ? N : M) ──
    if op == 'csel' and len(a)==4:
        cnd = a[3]
        if cnd not in COND: return None
        return f"{R(a[0])} = ({COND[cnd]}) ? {R(a[1])} : {R(a[2])};"
    # ── csinc wD, wN, wM, cond  (D = cond ? N : M+1) ──
    if op == 'csinc' and len(a)==4:
        cnd = a[3]
        if cnd not in COND: return None
        return f"{R(a[0])} = ({COND[cnd]}) ? {R(a[1])} : ({R(a[2])} + 1);"
    # ── madd rd,rn,rm,ra = rn*rm+ra ──
    if op == 'madd' and len(a)==4:
        return f"{R(a[0])} = {R(a[1])} * {R(a[2])} + {R(a[3])};"
    # ── ldrb (访存, 走 tlb) ──
    if op == 'ldrb':
        m = re.match(r'\[(\w+),\s*(\w+)\]', args_join(a[1:]))
        if m:
            base, idx = R(m.group(1)), R(m.group(2))
            return f"{R(a[0])} = tlb_read8(tlb, {base} + {idx});"
    # ── cmp (设 flag). Store both signed+unsigned operands so all conditions
    #    resolve correctly. w-regs zero-extend (unsigned 32-bit compare). ──
    if op == 'cmp' and len(a) >= 2:
        # cmp Rn, #imm[, lsl #sh]   or   cmp Rn, Rm
        if a[1].startswith('#'):
            v = imm(a[1])
            if len(a) >= 3:                       # optional  lsl #sh
                shm = re.search(r'lsl\s*#(\d+)', args_join(a[2:]))
                if shm: v <<= int(shm.group(1))
            rhs = f"{v}ULL"
        else:
            rhs = R(a[1])
        lhs = R(a[0])
        if is_w(a[0]): lhs, rhs = w32(lhs), w32(rhs)
        return f"FLAG_CMP({lhs}, {rhs});"
    # ── 条件分支 b.<cond> ──
    if op.startswith('b.') and op[2:] in COND:
        tgt = re.search(r'([0-9a-f]+)\s+<', args_join(a))
        if tgt: return f"if ({COND[op[2:]]}) goto L_{tgt.group(1)};"
    # ── cset wD, <cond> (D = cond ? 1 : 0) ──
    if op == 'cset' and len(a)==2 and a[1] in COND:
        return f"{R(a[0])} = ({COND[a[1]]}) ? 1 : 0;"
    # ── csinc / csel could go here later ──
    if op == 'cbz' and len(a)>=2:
        tgt = re.search(r'([0-9a-f]+)\s+<', args_join(a))
        v = w32(R(a[0])) if is_w(a[0]) else R(a[0])
        if tgt: return f"if (({v})==0) goto L_{tgt.group(1)};"
    if op == 'cbnz' and len(a)>=2:
        tgt = re.search(r'([0-9a-f]+)\s+<', args_join(a))
        v = w32(R(a[0])) if is_w(a[0]) else R(a[0])
        if tgt: return f"if (({v})!=0) goto L_{tgt.group(1)};"
    if op == 'b':
        tgt = re.search(r'([0-9a-f]+)\s+<', args_join(a))
        if tgt: return f"goto L_{tgt.group(1)};"
    # ── ret ──
    if op == 'ret':
        return "return;"
    # ── 忽略的指令 (栈操作/profiling/frame) ──
    #   sub sp / stp / ldp / adrp / str/ldr 到栈 / bl clock_gettime
    #   这些是 profiling 和 ABI 栈帧, 不影响 hash 结果. 谨慎跳过.
    if op in ('sub','add') and len(a)>=1 and reg(a[0])==32:  # sp 调整
        return "/* skip sp adjust */"
    if op == 'nop':
        return "/* nop */"
    # Everything else — stp/ldp/str/ldr (stack/global spills), adrp (global
    # addresses), bl/blr (calls), tail-call `b` to another function — is NOT
    # safely translatable yet. Reject rather than emit a silently-wrong spec.
    return None

def args_join(a): return ', '.join(a)

if __name__ == '__main__':
    lines = open(sys.argv[1]).readlines()
    body = translate(lines)
    if body is None:
        sys.stderr.write("转译失败 (有不支持的指令)\n"); sys.exit(1)
    print("// auto-generated by translate.py")
    for l in body: print(l)
