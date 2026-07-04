#!/usr/bin/env python3
"""
最小 ARM64→C 转译器 (阶段2 PoC).
输入: objdump 反汇编; 输出: 等价 C spec_fn (读写 cpu->regs[], 访存走 tlb).
支持 hash_bytes 用到的指令子集. 遇到不支持的指令 → 报错 (安全, 不静默出错).
"""
import re, sys

_ic_counter = [0]
def _next_ic_id():
    _ic_counter[0] += 1
    return _ic_counter[0]

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
    _ic_counter[0] = 0
    out = []
    labels = {}   # addr → C label (only for IN-FUNCTION targets)
    body = []
    insns = []
    for ln in lines:
        m = re.match(r'\s*([0-9a-f]+):\s+[0-9a-f]+\s+(\S+)\s*(.*)', ln)
        if not m: continue
        addr, op, args = int(m.group(1),16), m.group(2), m.group(3).strip()
        insns.append((addr, op, args))
    # Function address range = [first insn, last insn + 4). A branch to a target
    # inside this range is a local jump (goto); a target OUTSIDE it is a tail
    # call to another function (set guest PC, return — the dispatch loop runs
    # the callee, which returns to our guest LR).
    lo = insns[0][0] if insns else 0
    hi = insns[-1][0] + 4 if insns else 0
    for addr, op, args in insns:
        bm = re.search(r'([0-9a-f]+)\s+<', args)
        if bm and (op.startswith('b') or op in ('cbz','cbnz','tbz','tbnz')):
            t = int(bm.group(1), 16)
            if lo <= t < hi:                 # only label in-function targets
                labels[t] = f"L_{bm.group(1)}"

    for i, (addr, op, args) in enumerate(insns):
        if addr in labels:
            body.append(f"{labels[addr]}:")
        a = [x.strip() for x in args.split(',')] if args else []
        next_pc = insns[i+1][0] if i+1 < len(insns) else addr+4  # for bl return addr
        c = emit(addr, op, a, labels, next_pc)
        if c is None:
            sys.stderr.write(f"UNSUPPORTED @ {addr:#x}: {op} {args}\n")
            return None
        body.append("    " + c if not c.endswith(":") else c)
    return body

def emit(addr, op, a, labels, next_pc=None):
    """一条指令 → C 语句 (或 None=不支持)"""
    # ldur/stur are unscaled-offset ldr/str; identical semantics here.
    _unscaled = {'ldur':'ldr','stur':'str','ldurb':'ldrb','sturb':'strb',
                 'ldurh':'ldrh','sturh':'strh'}
    op = _unscaled.get(op, op)
    def branch_to(tgt_hex):
        """In-function target → 'goto L_x'; out-of-function → tail call."""
        t = int(tgt_hex, 16)
        if t in labels:
            return f"goto L_{tgt_hex}"
        return f"{{ cpu->regs[30] = PB_BASE + 0x{tgt_hex}ULL; return; }}"
    # 立即数
    def imm(s):
        s = s.strip().lstrip('#')
        s = s.split()[0].split('//')[0].strip()  # 去注释
        return int(s, 0)
    # AArch64 condition codes → C flag macros (defined in the generated spec).
    COND = {'eq':'FLAG_EQ','ne':'FLAG_NE','gt':'FLAG_GT','le':'FLAG_LE',
            'ge':'FLAG_GE','lt':'FLAG_LT','hi':'FLAG_HI','lo':'FLAG_LO',
            'hs':'FLAG_HS','ls':'FLAG_LS','cs':'FLAG_HS','cc':'FLAG_LO'}
    # ── adrp xD, <target>  (PC-relative page address of a global) ──
    #   objdump prints the file-absolute target; at runtime it's PB_BASE +
    #   that target (PB_BASE = library load base, set in the generated spec).
    if op == 'adrp' and len(a) == 2:
        tgt = re.match(r'0x([0-9a-f]+)', a[1].split()[0])
        if tgt:
            return f"{R(a[0])} = PB_BASE + 0x{tgt.group(1)}ULL;"
    # ── adr xD, <target>  (PC-relative byte address) ──
    if op == 'adr' and len(a) == 2:
        tgt = re.match(r'0x([0-9a-f]+)', a[1].split()[0])
        if tgt:
            return f"{R(a[0])} = PB_BASE + 0x{tgt.group(1)}ULL;"
    # ── 符号/零扩展 sxtw/sxth/sxtb, uxtw/uxth/uxtb ──
    if op in ('sxtw','sxth','sxtb','uxtw','uxth','uxtb') and len(a)==2:
        bits={'w':32,'h':16,'b':8}[op[3]]
        src=R(a[1])
        if op[0]=='s':
            res=f"((uint64_t)(int64_t)(int{bits}_t)({src}))"
        else:
            mask=(1<<bits)-1
            res=f"(({src}) & 0x{mask:x}ULL)"
        return f"{R(a[0])} = {res};"
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
    # ── 算术. The *s forms (adds/subs/ands) also set flags; bic clears bits.
    #    Shifted register operand supported (e.g. eor x0,x0,x0,lsr #31). ──
    ARI = {'add':'+','sub':'-','eor':'^','orr':'|','and':'&','mul':'*',
           'adds':'+','subs':'-','ands':'&','bic':'&~','eon':'^~'}
    if op in ARI and len(a) in (3,4):
        cop = ARI[op]
        if a[2].startswith('#'):
            rhs = f"{imm(a[2])}ULL"
        else:
            rhs = R(a[2])
        if len(a) == 4:                       # shifted register operand
            sm = re.match(r'(lsl|lsr|asr|ror)\s*#(\d+)', a[3])
            if not sm: return None
            sop, sh = sm.group(1), int(sm.group(2))
            if sop == 'lsl': rhs = f"({rhs} << {sh})"
            elif sop == 'lsr': rhs = f"({rhs} >> {sh})"
            elif sop == 'ror': rhs = f"ror64({rhs}, {sh})"
            else: rhs = f"((uint64_t)((int64_t){rhs} >> {sh}))"
        if cop == '&~': expr = f"{R(a[1])} & ~({rhs})"
        elif cop == '^~': expr = f"{R(a[1])} ^ ~({rhs})"
        else: expr = f"{R(a[1])} {cop} {rhs}"
        if is_w(a[0]): expr = w32(expr)
        code = f"{R(a[0])} = {expr};"
        if op in ('adds','subs','ands'):      # set flags from the result
            code += f" FLAG_CMP({R(a[0])}, 0);"
        return code
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
    # ── ldrb/strb (byte) and ldrh/strh (halfword). Two forms: [Xn, Xm{,lsl#s}]
    #    (register offset, e.g. table lookup) and [Xn, #imm] (imm / pre / post). ──
    if op in ('ldrb','strb','ldrh','strh'):
        ld  = op.startswith('ldr')
        LD, ST = ('PB_LDRB','PB_STRB') if op[-1]=='b' else ('PB_LDRH','PB_STRH')
        m = re.match(r'\[(\w+),\s*([wx]\w+)(?:,\s*lsl\s*#(\d+))?\]', args_join(a[1:]))
        if m:
            base, idx = R(m.group(1)), R(m.group(2))
            sh = int(m.group(3)) if m.group(3) else 0
            ea = f"({base} + ({idx} << {sh}))" if sh else f"({base} + {idx})"
            return f"{LD}({R(a[0])}, {ea});" if ld else f"{ST}({ea}, {R(a[0])});"
        r = _memop(a[1:])
        if r:
            base, off, pre, post = r
            code = f"{base} += {off}; " if pre else ""
            ea = base if (pre or post) else f"({base} + {off})"
            code += (f"{LD}({R(a[0])}, {ea});" if ld else f"{ST}({ea}, {R(a[0])});")
            if post: code += f" {base} += {off};"
            return code
    # ── ldr/str Xt/Wt, [Xn, Xm{, lsl #sh}]  (register-offset addressing) ──
    if op in ('ldr','str') and len(a) >= 2:
        mem = args_join(a[1:]).strip()
        m = re.match(r'\[(\w+),\s*(\w+)(?:,\s*lsl\s*#(\d+))?\]', mem)
        if m and not m.group(2).startswith('#'):
            base, idx = R(m.group(1)), R(m.group(2))
            sh = int(m.group(3)) if m.group(3) else 0
            ea = f"({base} + ({idx} << {sh}))" if sh else f"({base} + {idx})"
            w = is_w(a[0])
            if op == 'ldr': return (f"PB_LDRW({R(a[0])}, {ea});" if w
                                    else f"PB_LDR({R(a[0])}, {ea});")
            else:           return (f"PB_STRW({ea}, {R(a[0])});" if w
                                    else f"PB_STR({ea}, {R(a[0])});")
    # ── ldr/str Xt/Wt, [Xn, #imm]   (64/32-bit load/store via TLB) ──
    #    Also handles the stack-frame stp/ldp below. mem is the guest address
    #    space; sp lives in cpu->sp (index 32 → "SP").
    if op in ('ldr','str') and len(a) >= 2:
        r = _memop(a[1:])
        if r:
            base, off, wb_pre, wb_post = r
            code = ""
            if wb_pre: code += f"{base} += {off}; "
            ea = base if (wb_pre or wb_post) else f"({base} + {off})"
            w = is_w(a[0])                      # 32-bit access?
            if op == 'ldr': code += (f"PB_LDRW({R(a[0])}, {ea});" if w
                                     else f"PB_LDR({R(a[0])}, {ea});")
            else:           code += (f"PB_STRW({ea}, {R(a[0])});" if w
                                     else f"PB_STR({ea}, {R(a[0])});")
            if wb_post: code += f" {base} += {off};"
            return code
    # ── stp/ldp Xt1, Xt2, [Xn, #imm]  (pair load/store; pre/post-index) ──
    if op in ('stp','ldp') and len(a) >= 3:
        r = _memop(a[2:])
        if r:
            base, off, wb_pre, wb_post = r
            code = ""
            if wb_pre: code += f"{base} += {off}; "
            ea = base if (wb_pre or wb_post) else f"({base} + {off})"
            if op == 'ldp':
                code += f"PB_LDR({R(a[0])}, {ea}); PB_LDR({R(a[1])}, {ea} + 8);"
            else:
                code += f"PB_STR({ea}, {R(a[0])}); PB_STR({ea} + 8, {R(a[1])});"
            if wb_post: code += f" {base} += {off};"
            return code
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
    # ── cmn Rn, #imm  (compare negative: flags of Rn + imm) ──
    if op == 'cmn' and len(a) == 2:
        rhs = f"{imm(a[1])}ULL" if a[1].startswith('#') else R(a[1])
        lhs = R(a[0])
        # Z/N of (lhs + rhs): model as compare of lhs against -rhs.
        return f"FLAG_CMP({lhs}, (uint64_t)(-(int64_t)({rhs})));"
    # ── tst Rn, Rm/#imm  (flags of Rn & op2; only Z/N meaningful) ──
    if op == 'tst' and len(a) >= 2:
        rhs = f"{imm(a[1])}ULL" if a[1].startswith('#') else R(a[1])
        lhs = R(a[0])
        if is_w(a[0]): lhs, rhs = w32(lhs), w32(rhs)
        return f"FLAG_CMP(({lhs}) & ({rhs}), 0);"
    # ── 条件分支 b.<cond> ──
    if op.startswith('b.') and op[2:] in COND:
        tgt = re.search(r'([0-9a-f]+)\s+<', args_join(a))
        if tgt: return f"if ({COND[op[2:]]}) " + branch_to(tgt.group(1)) + ";"
    # ── tbnz/tbz Rn, #bit, tgt  (test single bit and branch) ──
    if op in ('tbnz','tbz') and len(a) >= 3:
        bit = imm(a[1])
        tgt = re.search(r'([0-9a-f]+)\s+<', args_join(a[2:]))
        if tgt:
            cond = f"(({R(a[0])} >> {bit}) & 1)"
            if op == 'tbz': cond = f"!{cond}"
            return f"if ({cond}) " + branch_to(tgt.group(1)) + ";"
    # ── cset wD, <cond> (D = cond ? 1 : 0) ──
    if op == 'cset' and len(a)==2 and a[1] in COND:
        return f"{R(a[0])} = ({COND[a[1]]}) ? 1 : 0;"
    # ── ubfx/ubfiz  (unsigned bitfield extract / insert-in-zero) ──
    #   ubfx Wd, Wn, #lsb, #width  → Wd = (Wn >> lsb) & ((1<<width)-1)
    #   ubfiz Wd, Wn, #lsb, #width → Wd = (Wn & ((1<<width)-1)) << lsb
    if op in ('ubfx','ubfiz') and len(a) == 4:
        lsb, width = imm(a[2]), imm(a[3])
        mask = (1 << width) - 1
        if op == 'ubfx':
            res = f"(({R(a[1])} >> {lsb}) & 0x{mask:x}ULL)"
        else:
            res = f"(({R(a[1])} & 0x{mask:x}ULL) << {lsb})"
        if is_w(a[0]): res = w32(res)
        return f"{R(a[0])} = {res};"
    # ── sbfx Wd, Wn, #lsb, #width  (signed bitfield extract) ──
    if op == 'sbfx' and len(a) == 4:
        lsb, width = imm(a[2]), imm(a[3])
        # sign-extend the width-bit field
        sh = 64 - width
        res = f"((uint64_t)(((int64_t)({R(a[1])} << ({sh}-{lsb})) ) >> {sh}))"
        if is_w(a[0]): res = w32(res)
        return f"{R(a[0])} = {res};"
    # ── bfxil Wd, Wn, #lsb, #width  (insert Wn[lsb+:width] into Wd low bits) ──
    if op == 'bfxil' and len(a) == 4:
        lsb, width = imm(a[2]), imm(a[3])
        mask = (1 << width) - 1
        field = f"(({R(a[1])} >> {lsb}) & 0x{mask:x}ULL)"
        res = f"(({R(a[0])} & ~0x{mask:x}ULL) | {field})"
        if is_w(a[0]): res = w32(res)
        return f"{R(a[0])} = {res};"
    # ── bfi Wd, Wn, #lsb, #width  (insert Wn[0+:width] into Wd[lsb+:width]) ──
    if op == 'bfi' and len(a) == 4:
        lsb, width = imm(a[2]), imm(a[3])
        mask = (1 << width) - 1
        field = f"(({R(a[1])} & 0x{mask:x}ULL) << {lsb})"
        res = f"(({R(a[0])} & ~(0x{mask:x}ULL << {lsb})) | {field})"
        if is_w(a[0]): res = w32(res)
        return f"{R(a[0])} = {res};"
    # ── lsl/lsr by immediate can appear as ubfm; handled via lsl/lsr above ──
    if op == 'cbz' and len(a)>=2:
        tgt = re.search(r'([0-9a-f]+)\s+<', args_join(a))
        v = w32(R(a[0])) if is_w(a[0]) else R(a[0])
        if tgt: return f"if (({v})==0) " + branch_to(tgt.group(1)) + ";"
    if op == 'cbnz' and len(a)>=2:
        tgt = re.search(r'([0-9a-f]+)\s+<', args_join(a))
        v = w32(R(a[0])) if is_w(a[0]) else R(a[0])
        if tgt: return f"if (({v})!=0) " + branch_to(tgt.group(1)) + ";"
    if op == 'b':
        tgt = re.search(r'([0-9a-f]+)\s+<', args_join(a))
        if tgt: return branch_to(tgt.group(1)) + ";"
    # ── bl <target>  (direct call). PB_CALL does inline-cache dispatch: if the
    #   callee has a translated spec_fn, call it directly (stays in host code,
    #   no interpreter round-trip); otherwise fall back to prebuilt_call (nested
    #   dispatch). Each call site gets its own static IC slot (unique id). ──
    if op == 'bl':
        tgt = re.search(r'([0-9a-f]+)\s+<', args_join(a))
        if tgt and next_pc is not None:
            ic = _next_ic_id()
            return (f"cpu->regs[30] = PB_BASE + 0x{next_pc:x}ULL; "
                    f"PB_CALL({ic}, cpu, tlb, PB_BASE + 0x{tgt.group(1)}ULL);")
    # ── blr xN  (indirect call). Target is a runtime register value. Inline
    #    cache is most valuable here: the target is usually monomorphic (e.g. a
    #    fixed allocator hook), so the IC hits ~always and skips the interpreter. ──
    if op == 'blr' and len(a) == 1 and next_pc is not None:
        ic = _next_ic_id()
        return (f"cpu->regs[30] = PB_BASE + 0x{next_pc:x}ULL; "
                f"PB_CALL({ic}, cpu, tlb, {R(a[0])});")
    # ── br xN  (indirect branch = tail call; does not return here) ──
    #   Set guest PC to the target and return from the spec_fn; the dispatch
    #   loop continues at the target (gadget_prebuilt_entry will NOT overwrite
    #   PC with LR because we set it explicitly). We emulate this by pointing
    #   cpu->regs[30] (which the trampoline copies to PC on return) at the tgt.
    if op == 'br' and len(a) == 1:
        return f"cpu->regs[30] = {R(a[0])}; return;"
    # ── ret ──
    if op == 'ret':
        return "return;"
    # ── ldrsb/ldrsh/ldrsw: sign-extended loads ──
    if op in ('ldrsb','ldrsh','ldrsw'):
        sz = {'ldrsb':(1,'int8_t'),'ldrsh':(2,'int16_t'),'ldrsw':(4,'int32_t')}[op]
        m = re.match(r'\[(\w+)(?:,\s*(\w+)(?:,\s*lsl\s*#(\d+))?)?\]', args_join(a[1:]))
        if m:
            base = R(m.group(1))
            ea = base
            if m.group(2) and not m.group(2).startswith('#'):
                sh = int(m.group(3)) if m.group(3) else 0
                ea = f"({base} + ({R(m.group(2))} << {sh}))" if sh else f"({base} + {R(m.group(2))})"
            n, ct = sz
            return (f"do {{ {ct} _s=0; tlb_read(tlb,{ea},&_s,{n}); "
                    f"{R(a[0])} = (uint64_t)(int64_t)_s; }} while(0);")
    # ── ccmp Rn, Rm/#imm, #nzcv, cond  (conditional compare) ──
    #   If cond holds, set flags from (Rn - op2); else set flags to #nzcv.
    #   We only need Z (eq/ne) and unsigned (hi/lo) downstream; model with a
    #   temporary compare when cond holds, otherwise the immediate nzcv's Z.
    if op == 'ccmp' and len(a) == 4 and a[3] in COND:
        rhs = f"{imm(a[1])}ULL" if a[1].startswith('#') else R(a[1])
        nzcv = imm(a[2])
        z_if_false = 1 if (nzcv & 0x4) else 0   # Z bit of the fallback nzcv
        # if cond: FLAG from (Rn,rhs); else emulate the given Z flag
        return (f"if ({COND[a[3]]}) {{ FLAG_CMP({R(a[0])}, {rhs}); }} "
                f"else {{ FLAG_CMP({z_if_false}? 0 : 1, 0); }}")
    # ── mrs/msr TPIDR_EL0: guest thread-local storage pointer (cpu->tls_ptr) ──
    if op == 'mrs' and len(a) == 2 and 'TPIDR_EL0' in a[1]:
        return f"{R(a[0])} = cpu->tls_ptr;"
    if op == 'msr' and len(a) == 2 and 'TPIDR_EL0' in a[0]:
        return f"cpu->tls_ptr = {R(a[1])};"
    # ── brk: breakpoint / unreachable (abort path). Real code only reaches it
    #    on an assertion failure; emit a trap so any surprise is loud, not silent.
    if op == 'brk':
        return "__builtin_trap();"
    if op == 'nop':
        return "/* nop */"
    # Everything else — stp/ldp/str/ldr (stack/global spills), adrp (global
    # addresses), bl/blr (calls), tail-call `b` to another function — is NOT
    # safely translatable yet. Reject rather than emit a silently-wrong spec.
    return None

def args_join(a): return ', '.join(a)

def _memop(mem_args):
    """Parse a memory operand from the args after the value register(s).
    Returns (base_expr, offset_int, writeback_pre, writeback_post) or None.
    Forms:  [Xn]            [Xn, #imm]       [Xn, #imm]!   (pre-index)
            [Xn], #imm  (post-index)"""
    s = args_join(mem_args).strip()
    # post-index:  [Xn], #imm
    m = re.match(r'\[(\w+)\],\s*#(-?\w+)', s)
    if m: return (R(m.group(1)), int(m.group(2), 0), False, True)
    # pre-index:   [Xn, #imm]!
    m = re.match(r'\[(\w+),\s*#(-?\w+)\]!', s)
    if m: return (R(m.group(1)), int(m.group(2), 0), True, False)
    # offset:      [Xn, #imm]   or   [Xn]
    m = re.match(r'\[(\w+),\s*#(-?\w+)\]', s)
    if m: return (R(m.group(1)), int(m.group(2), 0), False, False)
    m = re.match(r'\[(\w+)\]', s)
    if m: return (R(m.group(1)), 0, False, False)
    return None

if __name__ == '__main__':
    lines = open(sys.argv[1]).readlines()
    body = translate(lines)
    if body is None:
        sys.stderr.write("转译失败 (有不支持的指令)\n"); sys.exit(1)
    print("// auto-generated by translate.py")
    for l in body: print(l)
