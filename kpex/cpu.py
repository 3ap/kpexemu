"""Interpreter for the ARM940T CPU (ARMv4T architecture: ARM and Thumb instruction sets).

How it works
----------------
* Registers r0..r15 live in the list `cpu.r`. While an instruction runs,
  r[15] holds the "visible" PC value: instruction address + 8 in ARM state
  and + 4 in Thumb state, just like on the real CPU.
* The address of the next instruction is kept in `cpu.next_pc`. A branch
  instruction simply writes the new address there.
* The N, Z, C, V flags are separate fields (0 or 1): simpler and faster
  than packing and unpacking the CPSR register every time.
* Every 32-bit (or 16-bit for Thumb) instruction word is decoded once into
  a small handler function, which is then cached. The cache is keyed by the
  word value, not the address, so self-modifying code and code copied to RAM
  do not need a cache flush.
"""

M32 = 0xFFFFFFFF

# CPU modes (low 5 bits of CPSR)
USR, FIQ, IRQ, SVC, ABT, UND, SYS = 0x10, 0x11, 0x12, 0x13, 0x17, 0x1B, 0x1F
MODE_NAMES = {USR: 'usr', FIQ: 'fiq', IRQ: 'irq', SVC: 'svc', ABT: 'abt', UND: 'und', SYS: 'sys'}
# Register bank number for r13/r14/SPSR in each mode (usr and sys share one)
BANK = {USR: 0, SYS: 0, FIQ: 1, IRQ: 2, SVC: 3, ABT: 4, UND: 5}

# Exception vectors
VEC_RESET, VEC_UND, VEC_SWI, VEC_PABT, VEC_DABT, VEC_IRQ, VEC_FIQ = 0x00, 0x04, 0x08, 0x0C, 0x10, 0x18, 0x1C


def ror(val, amount):
    amount &= 31
    if amount == 0:
        return val
    return ((val >> amount) | (val << (32 - amount))) & M32


def sext(val, bits):
    """Sign-extend a `bits`-bit number to a plain int."""
    sign = 1 << (bits - 1)
    return (val & (sign - 1)) - (val & sign)


# ---------------------------------------------------------------------------
# Shifts (barrel shifter). They return (result, carry).

LSL, LSR, ASR, ROR = 0, 1, 2, 3


def shift_imm(val, kind, amount, carry):
    """Shift by a constant encoded in the instruction (0..31, with special cases for 0)."""
    if kind == LSL:
        if amount == 0:
            return val, carry
        return (val << amount) & M32, (val >> (32 - amount)) & 1
    if kind == LSR:
        if amount == 0:  # LSR #0 means LSR #32
            return 0, val >> 31
        return val >> amount, (val >> (amount - 1)) & 1
    if kind == ASR:
        if amount == 0:  # ASR #0 means ASR #32
            return (M32 if val >> 31 else 0), val >> 31
        return (sext(val, 32) >> amount) & M32, (val >> (amount - 1)) & 1
    # ROR
    if amount == 0:  # ROR #0 means RRX: shift by 1 through the carry flag
        return (carry << 31) | (val >> 1), val & 1
    return ror(val, amount), (val >> (amount - 1)) & 1


def shift_reg(val, kind, amount, carry):
    """Shift by a register value (its low byte is used, 0..255)."""
    if amount == 0:
        return val, carry
    if kind == LSL:
        if amount < 32:
            return (val << amount) & M32, (val >> (32 - amount)) & 1
        if amount == 32:
            return 0, val & 1
        return 0, 0
    if kind == LSR:
        if amount < 32:
            return val >> amount, (val >> (amount - 1)) & 1
        if amount == 32:
            return 0, val >> 31
        return 0, 0
    if kind == ASR:
        if amount < 32:
            return (sext(val, 32) >> amount) & M32, (val >> (amount - 1)) & 1
        return (M32 if val >> 31 else 0), val >> 31
    # ROR
    amount &= 31
    if amount == 0:
        return val, val >> 31
    return ror(val, amount), (val >> (amount - 1)) & 1


# ---------------------------------------------------------------------------
# Arithmetic and logic operations, shared by ARM and Thumb.
# Signature: op(cpu, a, b, shifter_carry, set_flags) -> result or None
# (None means "the result is not written anywhere": TST, TEQ, CMP, CMN).

def _logic_flags(cpu, res, sc):
    cpu.n = res >> 31
    cpu.z = 1 if res == 0 else 0
    cpu.c = sc


def _sub_flags(cpu, a, b, res, carry):
    cpu.n = res >> 31
    cpu.z = 1 if res == 0 else 0
    cpu.c = carry
    cpu.v = ((a ^ b) & (a ^ res)) >> 31


def _add_flags(cpu, a, b, full):
    res = full & M32
    cpu.n = res >> 31
    cpu.z = 1 if res == 0 else 0
    cpu.c = full >> 32
    cpu.v = ((a ^ res) & (b ^ res)) >> 31
    return res


def op_and(cpu, a, b, sc, s):
    res = a & b
    if s: _logic_flags(cpu, res, sc)
    return res


def op_eor(cpu, a, b, sc, s):
    res = a ^ b
    if s: _logic_flags(cpu, res, sc)
    return res


def op_sub(cpu, a, b, sc, s):
    res = (a - b) & M32
    if s: _sub_flags(cpu, a, b, res, 1 if a >= b else 0)
    return res


def op_rsb(cpu, a, b, sc, s):
    return op_sub(cpu, b, a, sc, s)


def op_add(cpu, a, b, sc, s):
    if s: return _add_flags(cpu, a, b, a + b)
    return (a + b) & M32


def op_adc(cpu, a, b, sc, s):
    full = a + b + cpu.c
    if s: return _add_flags(cpu, a, b, full)
    return full & M32


def op_sbc(cpu, a, b, sc, s):
    borrow = 1 - cpu.c
    res = (a - b - borrow) & M32
    if s: _sub_flags(cpu, a, b, res, 1 if a >= b + borrow else 0)
    return res


def op_rsc(cpu, a, b, sc, s):
    return op_sbc(cpu, b, a, sc, s)


def op_tst(cpu, a, b, sc, s):
    _logic_flags(cpu, a & b, sc)


def op_teq(cpu, a, b, sc, s):
    _logic_flags(cpu, a ^ b, sc)


def op_cmp(cpu, a, b, sc, s):
    _sub_flags(cpu, a, b, (a - b) & M32, 1 if a >= b else 0)


def op_cmn(cpu, a, b, sc, s):
    _add_flags(cpu, a, b, a + b)


def op_orr(cpu, a, b, sc, s):
    res = a | b
    if s: _logic_flags(cpu, res, sc)
    return res


def op_mov(cpu, a, b, sc, s):
    if s: _logic_flags(cpu, b, sc)
    return b


def op_bic(cpu, a, b, sc, s):
    res = a & ~b & M32
    if s: _logic_flags(cpu, res, sc)
    return res


def op_mvn(cpu, a, b, sc, s):
    res = ~b & M32
    if s: _logic_flags(cpu, res, sc)
    return res


DP_OPS = [op_and, op_eor, op_sub, op_rsb, op_add, op_adc, op_sbc, op_rsc,
          op_tst, op_teq, op_cmp, op_cmn, op_orr, op_mov, op_bic, op_mvn]
DP_NAMES = ['and', 'eor', 'sub', 'rsb', 'add', 'adc', 'sbc', 'rsc',
            'tst', 'teq', 'cmp', 'cmn', 'orr', 'mov', 'bic', 'mvn']


# ---------------------------------------------------------------------------
# ARM940T CP15 coprocessor: caches and the memory protection unit (MPU).
# Memory protection is not emulated, we only remember register values.

class CP15:
    ID = 0x41129400  # ARM940T

    def __init__(self, cpu):
        self.cpu = cpu
        self.regs = {}
        self.control = 0x78

    def read(self, opc1, crn, crm, opc2):
        if crn == 0:
            return self.ID if opc2 == 0 else 0x0F0F0F0F  # ID / cache type
        if crn == 1:
            return self.control
        return self.regs.get((crn, crm, opc2), 0)

    def write(self, opc1, crn, crm, opc2, val):
        if crn == 1:
            self.control = val
        elif crn == 7 and crm == 0 and opc2 == 4:
            self.cpu.halted = True  # "wait for interrupt"
        else:
            self.regs[(crn, crm, opc2)] = val

    @property
    def high_vectors(self):
        return bool(self.control & (1 << 13))


# ---------------------------------------------------------------------------

class CPU:
    def __init__(self, bus):
        self.bus = bus
        self.r = [0] * 16
        self.n = self.z = self.c = self.v = 0
        self.i = 1          # IRQ disabled
        self.f = 1          # FIQ disabled
        self.t = 0          # 1 = Thumb state
        self.mode = SVC
        self.banked = [[0, 0] for _ in range(6)]   # r13, r14 for each bank
        self.spsr_bank = [0] * 6
        self.usr_r8_12 = [0] * 5
        self.fiq_r8_12 = [0] * 5
        self.next_pc = 0
        self.cp15 = CP15(self)
        self.halted = False
        self.irq_line = False   # set by the interrupt controller
        self.fiq_line = False
        self.cycles = 0
        self.arm_cache = {}
        self.thumb_cache = {}
        # Optional handler called on an unknown instruction
        self.on_undefined = None

    # --- status register -------------------------------------------------

    @property
    def cpsr(self):
        return ((self.n << 31) | (self.z << 30) | (self.c << 29) | (self.v << 28) |
                (self.i << 7) | (self.f << 6) | (self.t << 5) | self.mode)

    def set_cpsr(self, val):
        self.n = (val >> 31) & 1
        self.z = (val >> 30) & 1
        self.c = (val >> 29) & 1
        self.v = (val >> 28) & 1
        self.i = (val >> 7) & 1
        self.f = (val >> 6) & 1
        self.t = (val >> 5) & 1
        self.set_mode(val & 0x1F)

    @property
    def spsr(self):
        bank = BANK.get(self.mode, 0)
        return self.spsr_bank[bank] if bank else self.cpsr

    @spsr.setter
    def spsr(self, val):
        bank = BANK.get(self.mode, 0)
        if bank:
            self.spsr_bank[bank] = val & M32

    def set_mode(self, new):
        old = self.mode
        if new == old:
            return
        if new not in BANK:
            new = SYS  # invalid mode; unpredictable on real hardware
        r = self.r
        ob, nb = BANK[old], BANK[new]
        if ob != nb:
            self.banked[ob][0] = r[13]
            self.banked[ob][1] = r[14]
            if old == FIQ:
                self.fiq_r8_12[:] = r[8:13]
                r[8:13] = self.usr_r8_12
            elif new == FIQ:
                self.usr_r8_12[:] = r[8:13]
                r[8:13] = self.fiq_r8_12
            r[13], r[14] = self.banked[nb]
        self.mode = new

    def user_reg(self, n):
        """User-mode register (needed for LDM/STM with the S bit)."""
        if BANK[self.mode] == 0 or n < 8 or n == 15:
            return self.r[n]
        if n < 13:
            return self.usr_r8_12[n - 8] if self.mode == FIQ else self.r[n]
        return self.banked[0][n - 13]

    def set_user_reg(self, n, val):
        if BANK[self.mode] == 0 or n < 8:
            self.r[n] = val
        elif n < 13:
            if self.mode == FIQ:
                self.usr_r8_12[n - 8] = val
            else:
                self.r[n] = val
        elif n < 15:
            self.banked[0][n - 13] = val

    # --- conditions ------------------------------------------------------------

    def check(self, cond):
        if cond == 0x0: return self.z
        if cond == 0x1: return not self.z
        if cond == 0x2: return self.c
        if cond == 0x3: return not self.c
        if cond == 0x4: return self.n
        if cond == 0x5: return not self.n
        if cond == 0x6: return self.v
        if cond == 0x7: return not self.v
        if cond == 0x8: return self.c and not self.z
        if cond == 0x9: return not self.c or self.z
        if cond == 0xA: return self.n == self.v
        if cond == 0xB: return self.n != self.v
        if cond == 0xC: return not self.z and self.n == self.v
        if cond == 0xD: return self.z or self.n != self.v
        if cond == 0xE: return True
        return False  # 0xF: "never" in ARMv4

    # --- branches and exceptions ---------------------------------------------

    def branch(self, addr):
        self.next_pc = addr & (0xFFFFFFFE if self.t else 0xFFFFFFFC)

    def branch_exchange(self, addr):
        """BX: bit 0 of the address selects the state (1 = Thumb)."""
        self.t = addr & 1
        self.next_pc = addr & (0xFFFFFFFE if self.t else 0xFFFFFFFC)

    def return_from_exception(self, addr):
        """Write to PC restoring CPSR from SPSR (MOVS pc, ... / LDM ...^)."""
        self.set_cpsr(self.spsr)
        self.branch(addr)

    def exception(self, vector, mode, lr):
        cpsr = self.cpsr
        self.set_mode(mode)
        self.spsr_bank[BANK[mode]] = cpsr
        self.r[14] = lr & M32
        self.t = 0
        self.i = 1
        if mode == FIQ or vector == VEC_RESET:
            self.f = 1
        self.halted = False
        self.next_pc = (0xFFFF0000 if self.cp15.high_vectors else 0) + vector

    def undefined(self, instr):
        if self.on_undefined:
            self.on_undefined(self, instr)
        pc = self.next_pc - (2 if self.t else 4)
        self.exception(VEC_UND, UND, pc + (2 if self.t else 4))

    # --- main loop -----------------------------------------------------------

    def run(self, count):
        """Run up to `count` instructions. Returns the number executed."""
        r = self.r
        bus = self.bus
        read32 = bus.read32
        read16 = bus.read16
        arm_cache = self.arm_cache
        thumb_cache = self.thumb_cache
        check = self.check
        done = 0
        while done < count:
            if self.fiq_line and not self.f:
                self.exception(VEC_FIQ, FIQ, self.next_pc + 4)
            elif self.irq_line and not self.i:
                self.exception(VEC_IRQ, IRQ, self.next_pc + 4)
            elif self.halted:
                break
            pc = self.next_pc
            if self.t:
                instr = read16(pc)
                self.next_pc = pc + 2
                r[15] = pc + 4
                fn = thumb_cache.get(instr)
                if fn is None:
                    fn = thumb_cache[instr] = decode_thumb(instr)
                fn(self)
            else:
                instr = read32(pc)
                self.next_pc = pc + 4
                r[15] = pc + 8
                cond = instr >> 28
                if cond == 0xE or check(cond):
                    fn = arm_cache.get(instr)
                    if fn is None:
                        fn = arm_cache[instr] = decode_arm(instr)
                    fn(self)
            done += 1
        self.cycles += done
        r[15] = self.next_pc
        return done

    @property
    def pc(self):
        """Address of the next instruction to be executed."""
        return self.next_pc

    def dump(self):
        r = self.r
        lines = []
        for row in range(4):
            lines.append('  '.join(f'r{n:<2}={r[n] & M32:08x}' for n in range(row * 4, row * 4 + 4)
                                   if n != 15))
        flags = ''.join(ch if bit else '-' for ch, bit in zip('NZCVIFT', (self.n, self.z, self.c, self.v,
                                                                       self.i, self.f, self.t)))
        lines.append(f'pc ={self.next_pc:08x}  cpsr={self.cpsr:08x} [{flags} {MODE_NAMES.get(self.mode, "?")}]')
        return '\n'.join(lines)


# ===========================================================================
# ARM instruction decoder. Every make_* function returns a handler fn(cpu).

def decode_arm(instr):
    if (instr & 0x0FFFFFF0) == 0x012FFF10:
        return make_bx(instr)
    if (instr & 0x0FC000F0) == 0x00000090:
        return make_mul(instr)
    if (instr & 0x0F8000F0) == 0x00800090:
        return make_mull(instr)
    if (instr & 0x0FB00FF0) == 0x01000090:
        return make_swp(instr)
    if (instr & 0x0E000090) == 0x00000090:
        return make_ldst_half(instr)
    if (instr & 0x0FBF0FFF) == 0x010F0000:
        return make_mrs(instr)
    if (instr & 0x0DB0F000) == 0x0120F000:
        return make_msr(instr)
    kind = (instr >> 25) & 7
    if kind in (0, 1):
        if (instr & 0x01900000) == 0x01000000:
            # TST/TEQ/CMP/CMN without the S bit: this is the "miscellaneous"
            # instruction space (CLZ, BLX etc. in ARMv5). ARMv4 has none of these.
            return make_undefined(instr)
        return make_dp(instr)
    if kind in (2, 3):
        if kind == 3 and instr & 0x10:
            return make_undefined(instr)
        return make_ldst(instr)
    if kind == 4:
        return make_ldm_stm(instr)
    if kind == 5:
        return make_branch(instr)
    if kind == 6:
        return make_undefined(instr)  # LDC/STC: there are no other coprocessors
    # kind == 7
    if instr & (1 << 24):
        return make_swi(instr)
    if instr & 0x10:
        return make_mcr_mrc(instr)
    return make_undefined(instr)


def make_undefined(instr):
    def ex(cpu):
        cpu.undefined(instr)
    return ex


def make_op2(instr):
    """Second operand of data processing. Returns a function cpu -> (value, carry)."""
    if instr & (1 << 25):
        rot = ((instr >> 8) & 0xF) * 2
        val = ror(instr & 0xFF, rot)
        if rot == 0:
            return lambda cpu: (val, cpu.c)
        carry = val >> 31
        return lambda cpu: (val, carry)
    rm = instr & 0xF
    kind = (instr >> 5) & 3
    if instr & 0x10:
        rs = (instr >> 8) & 0xF
        extra = 4 if rm == 15 else 0  # with a register shift PC reads as +12

        def op2_reg(cpu):
            r = cpu.r
            return shift_reg(r[rm] + extra, kind, r[rs] & 0xFF, cpu.c)
        return op2_reg
    amount = (instr >> 7) & 0x1F
    if kind == LSL and amount == 0:
        return lambda cpu: (cpu.r[rm], cpu.c)

    def op2_imm(cpu):
        return shift_imm(cpu.r[rm], kind, amount, cpu.c)
    return op2_imm


def make_dp(instr):
    op = (instr >> 21) & 0xF
    s = (instr >> 20) & 1
    rn = (instr >> 16) & 0xF
    rd = (instr >> 12) & 0xF
    op2 = make_op2(instr)
    fn = DP_OPS[op]
    rn_extra = 4 if (rn == 15 and not instr & (1 << 25) and instr & 0x10) else 0
    if rd != 15:
        def ex(cpu):
            b, sc = op2(cpu)
            res = fn(cpu, cpu.r[rn] + rn_extra, b, sc, s)
            if res is not None:
                cpu.r[rd] = res
        return ex
    if s:
        def ex_pc_s(cpu):
            b, sc = op2(cpu)
            res = fn(cpu, cpu.r[rn] + rn_extra, b, sc, 0)
            if res is not None:
                cpu.return_from_exception(res)
            else:
                cpu.set_cpsr(cpu.spsr)  # TSTP etc., a legacy of 26-bit ARMs
        return ex_pc_s

    def ex_pc(cpu):
        b, sc = op2(cpu)
        res = fn(cpu, cpu.r[rn] + rn_extra, b, sc, 0)
        if res is not None:
            cpu.branch(res)
    return ex_pc


def make_bx(instr):
    rm = instr & 0xF

    def ex(cpu):
        cpu.branch_exchange(cpu.r[rm])
    return ex


def make_branch(instr):
    offset = sext(instr & 0xFFFFFF, 24) << 2
    link = instr & (1 << 24)

    def ex(cpu):
        if link:
            cpu.r[14] = cpu.next_pc
        cpu.next_pc = (cpu.r[15] + offset) & M32
    return ex


def make_mul(instr):
    rd = (instr >> 16) & 0xF
    rn = (instr >> 12) & 0xF
    rs = (instr >> 8) & 0xF
    rm = instr & 0xF
    acc = instr & (1 << 21)
    s = instr & (1 << 20)

    def ex(cpu):
        r = cpu.r
        res = r[rm] * r[rs]
        if acc:
            res += r[rn]
        res &= M32
        r[rd] = res
        if s:
            cpu.n = res >> 31
            cpu.z = 1 if res == 0 else 0
    return ex


def make_mull(instr):
    rdhi = (instr >> 16) & 0xF
    rdlo = (instr >> 12) & 0xF
    rs = (instr >> 8) & 0xF
    rm = instr & 0xF
    signed = instr & (1 << 22)
    acc = instr & (1 << 21)
    s = instr & (1 << 20)

    def ex(cpu):
        r = cpu.r
        if signed:
            res = sext(r[rm], 32) * sext(r[rs], 32)
        else:
            res = r[rm] * r[rs]
        if acc:
            res += (r[rdhi] << 32) | r[rdlo]
        res &= 0xFFFFFFFFFFFFFFFF
        r[rdlo] = res & M32
        r[rdhi] = res >> 32
        if s:
            cpu.n = res >> 63
            cpu.z = 1 if res == 0 else 0
    return ex


def make_swp(instr):
    rn = (instr >> 16) & 0xF
    rd = (instr >> 12) & 0xF
    rm = instr & 0xF
    byte = instr & (1 << 22)

    def ex(cpu):
        r = cpu.r
        addr = r[rn]
        src = r[rm]
        if byte:
            val = cpu.bus.read8(addr)
            cpu.bus.write8(addr, src & 0xFF)
        else:
            val = ror(cpu.bus.read32(addr & ~3), (addr & 3) * 8)
            cpu.bus.write32(addr & ~3, src)
        r[rd] = val
    return ex


def make_mrs(instr):
    rd = (instr >> 12) & 0xF
    use_spsr = instr & (1 << 22)

    def ex(cpu):
        cpu.r[rd] = cpu.spsr if use_spsr else cpu.cpsr
    return ex


def make_msr(instr):
    use_spsr = instr & (1 << 22)
    mask = 0
    if instr & (1 << 16): mask |= 0x000000FF  # c: control (mode, I, F, T)
    if instr & (1 << 17): mask |= 0x0000FF00  # x
    if instr & (1 << 18): mask |= 0x00FF0000  # s
    if instr & (1 << 19): mask |= 0xFF000000  # f: flags
    if instr & (1 << 25):
        val = ror(instr & 0xFF, ((instr >> 8) & 0xF) * 2)
        get = lambda cpu: val
    else:
        rm = instr & 0xF
        get = lambda cpu: cpu.r[rm]

    def ex(cpu):
        val = get(cpu)
        if use_spsr:
            cpu.spsr = (cpu.spsr & ~mask) | (val & mask)
        else:
            m = mask if cpu.mode != USR else (mask & 0xFF000000)
            cpu.set_cpsr((cpu.cpsr & ~m) | (val & m))
    return ex


def make_ldst(instr):
    reg_offset = instr & (1 << 25)
    pre = instr & (1 << 24)
    up = instr & (1 << 23)
    byte = instr & (1 << 22)
    wback = (instr & (1 << 21)) or not pre   # post-indexing always writes the base back
    load = instr & (1 << 20)
    rn = (instr >> 16) & 0xF
    rd = (instr >> 12) & 0xF
    if reg_offset:
        rm = instr & 0xF
        kind = (instr >> 5) & 3
        amount = (instr >> 7) & 0x1F
        get_off = lambda cpu: shift_imm(cpu.r[rm], kind, amount, cpu.c)[0]
    else:
        imm = instr & 0xFFF
        get_off = lambda cpu: imm

    def ex(cpu):
        r = cpu.r
        bus = cpu.bus
        base = r[rn]
        off = get_off(cpu)
        moved = (base + off if up else base - off) & M32
        addr = moved if pre else base
        if load:
            if byte:
                val = bus.read8(addr)
            else:
                val = bus.read32(addr & ~3)
                if addr & 3:
                    val = ror(val, (addr & 3) * 8)
            if wback and rn != 15:
                r[rn] = moved
            if rd == 15:
                cpu.branch(val)
            else:
                r[rd] = val
        else:
            val = r[rd] + (4 if rd == 15 else 0)
            if byte:
                bus.write8(addr, val & 0xFF)
            else:
                bus.write32(addr & ~3, val & M32)
            if wback and rn != 15:
                r[rn] = moved
    return ex


def make_ldst_half(instr):
    pre = instr & (1 << 24)
    up = instr & (1 << 23)
    imm_offset = instr & (1 << 22)
    wback = (instr & (1 << 21)) or not pre
    load = instr & (1 << 20)
    rn = (instr >> 16) & 0xF
    rd = (instr >> 12) & 0xF
    sh = (instr >> 5) & 3
    if not load and sh != 1:
        return make_undefined(instr)  # LDRD/STRD only appeared in ARMv5TE
    if imm_offset:
        imm = ((instr >> 4) & 0xF0) | (instr & 0xF)
        get_off = lambda cpu: imm
    else:
        rm = instr & 0xF
        get_off = lambda cpu: cpu.r[rm]

    def ex(cpu):
        r = cpu.r
        bus = cpu.bus
        base = r[rn]
        off = get_off(cpu)
        moved = (base + off if up else base - off) & M32
        addr = moved if pre else base
        if load:
            if sh == 1:
                val = bus.read16(addr & ~1)
            elif sh == 2:
                val = sext(bus.read8(addr), 8) & M32
            else:
                val = sext(bus.read16(addr & ~1), 16) & M32
            if wback and rn != 15:
                r[rn] = moved
            if rd == 15:
                cpu.branch(val)
            else:
                r[rd] = val
        else:
            bus.write16(addr & ~1, (r[rd] + (4 if rd == 15 else 0)) & 0xFFFF)
            if wback and rn != 15:
                r[rn] = moved
    return ex


def make_ldm_stm(instr):
    pre = instr & (1 << 24)
    up = instr & (1 << 23)
    s_bit = instr & (1 << 22)
    wback = instr & (1 << 21)
    load = instr & (1 << 20)
    rn = (instr >> 16) & 0xF
    regs = [n for n in range(16) if instr & (1 << n)]
    count = len(regs) or 16  # empty list: unpredictable on real hardware
    has_pc = 15 in regs

    def ex(cpu):
        r = cpu.r
        bus = cpu.bus
        base = r[rn]
        if up:
            addr = base + 4 if pre else base
            new_base = (base + 4 * count) & M32
        else:
            addr = base - 4 * count + (0 if pre else 4)
            new_base = (base - 4 * count) & M32
        addr &= M32
        user_bank = s_bit and not (load and has_pc)
        if load:
            if wback:
                r[rn] = new_base
            for n in regs:
                val = bus.read32(addr & ~3)
                addr = (addr + 4) & M32
                if n == 15:
                    if s_bit:
                        cpu.return_from_exception(val)
                    else:
                        cpu.branch(val)
                elif user_bank:
                    cpu.set_user_reg(n, val)
                else:
                    r[n] = val
        else:
            for n in regs:
                if n == 15:
                    val = r[15] + 4
                elif user_bank:
                    val = cpu.user_reg(n)
                else:
                    val = r[n]
                bus.write32(addr & ~3, val & M32)
                addr = (addr + 4) & M32
            if wback:
                r[rn] = new_base
    return ex


def make_swi(instr):
    def ex(cpu):
        cpu.exception(VEC_SWI, SVC, cpu.next_pc)
    return ex


def make_mcr_mrc(instr):
    cp = (instr >> 8) & 0xF
    if cp != 15:
        return make_undefined(instr)
    opc1 = (instr >> 21) & 7
    to_arm = instr & (1 << 20)  # MRC
    crn = (instr >> 16) & 0xF
    rd = (instr >> 12) & 0xF
    opc2 = (instr >> 5) & 7
    crm = instr & 0xF

    def ex(cpu):
        if to_arm:
            val = cpu.cp15.read(opc1, crn, crm, opc2)
            if rd == 15:
                cpu.n, cpu.z, cpu.c, cpu.v = (val >> 31) & 1, (val >> 30) & 1, (val >> 29) & 1, (val >> 28) & 1
            else:
                cpu.r[rd] = val
        else:
            cpu.cp15.write(opc1, crn, crm, opc2, cpu.r[rd])
    return ex


# ===========================================================================
# Thumb instruction decoder (16 bits).

def decode_thumb(instr):
    top = instr >> 11
    if top < 3:
        return t_shift_imm(instr)
    if top == 3:
        return t_add_sub(instr)
    if top < 8:
        return t_imm8(instr)
    if (instr >> 10) == 0x10:
        return t_alu(instr)
    if (instr >> 10) == 0x11:
        return t_hireg(instr)
    if top == 9:
        return t_ldr_pc(instr)
    if (instr >> 12) == 5:
        return t_ldst_reg(instr) if not instr & (1 << 9) else t_ldst_sign(instr)
    if (instr >> 13) == 3:
        return t_ldst_imm(instr)
    if (instr >> 12) == 8:
        return t_ldst_half(instr)
    if (instr >> 12) == 9:
        return t_ldst_sp(instr)
    if (instr >> 12) == 0xA:
        return t_add_pc_sp(instr)
    if (instr >> 8) == 0xB0:
        return t_adjust_sp(instr)
    if (instr >> 12) == 0xB and (instr >> 9) & 3 == 2:
        return t_push_pop(instr)
    if (instr >> 12) == 0xC:
        return t_ldm_stm(instr)
    if (instr >> 12) == 0xD:
        cond = (instr >> 8) & 0xF
        if cond == 0xF:
            return t_swi(instr)
        if cond == 0xE:
            return make_undefined(instr)
        return t_bcond(instr)
    if top == 0x1C:
        return t_b(instr)
    if top == 0x1E:
        return t_bl_hi(instr)
    if top == 0x1F:
        return t_bl_lo(instr)
    return make_undefined(instr)


def t_shift_imm(instr):
    kind = (instr >> 11) & 3
    amount = (instr >> 6) & 0x1F
    rs = (instr >> 3) & 7
    rd = instr & 7

    def ex(cpu):
        res, c = shift_imm(cpu.r[rs], kind, amount, cpu.c)
        cpu.r[rd] = res
        _logic_flags(cpu, res, c)
    return ex


def t_add_sub(instr):
    imm = instr & (1 << 10)
    sub = instr & (1 << 9)
    val = (instr >> 6) & 7
    rs = (instr >> 3) & 7
    rd = instr & 7
    fn = op_sub if sub else op_add

    def ex(cpu):
        r = cpu.r
        r[rd] = fn(cpu, r[rs], val if imm else r[val], 0, 1)
    return ex


def t_imm8(instr):
    op = (instr >> 11) & 3
    rd = (instr >> 8) & 7
    imm = instr & 0xFF
    fn = (op_mov, op_cmp, op_add, op_sub)[op]

    def ex(cpu):
        res = fn(cpu, cpu.r[rd], imm, cpu.c, 1)
        if res is not None:
            cpu.r[rd] = res
    return ex


def t_alu(instr):
    op = (instr >> 6) & 0xF
    rs = (instr >> 3) & 7
    rd = instr & 7
    # 0 AND 1 EOR 2 LSL 3 LSR 4 ASR 5 ADC 6 SBC 7 ROR 8 TST 9 NEG A CMP B CMN C ORR D MUL E BIC F MVN
    if op in (2, 3, 4, 7):
        kind = {2: LSL, 3: LSR, 4: ASR, 7: ROR}[op]

        def ex_shift(cpu):
            r = cpu.r
            res, c = shift_reg(r[rd], kind, r[rs] & 0xFF, cpu.c)
            r[rd] = res
            _logic_flags(cpu, res, c)
        return ex_shift
    if op == 9:
        def ex_neg(cpu):
            cpu.r[rd] = op_sub(cpu, 0, cpu.r[rs], 0, 1)
        return ex_neg
    if op == 0xD:
        def ex_mul(cpu):
            r = cpu.r
            res = (r[rd] * r[rs]) & M32
            r[rd] = res
            cpu.n = res >> 31
            cpu.z = 1 if res == 0 else 0
        return ex_mul
    fn = {0: op_and, 1: op_eor, 5: op_adc, 6: op_sbc, 8: op_tst, 0xA: op_cmp,
          0xB: op_cmn, 0xC: op_orr, 0xE: op_bic, 0xF: op_mvn}[op]

    def ex(cpu):
        r = cpu.r
        res = fn(cpu, r[rd], r[rs], cpu.c, 1)
        if res is not None:
            r[rd] = res
    return ex


def t_hireg(instr):
    op = (instr >> 8) & 3
    rs = (instr >> 3) & 0xF        # includes the H2 bit
    rd = (instr & 7) | ((instr >> 4) & 8)  # includes the H1 bit
    if op == 3:
        def ex_bx(cpu):
            cpu.branch_exchange(cpu.r[rs])
        return ex_bx
    if op == 1:
        def ex_cmp(cpu):
            op_cmp(cpu, cpu.r[rd], cpu.r[rs], 0, 1)
        return ex_cmp

    def ex(cpu):
        r = cpu.r
        val = r[rs] if op == 2 else (r[rd] + r[rs]) & M32
        if rd == 15:
            cpu.branch(val)
        else:
            r[rd] = val
    return ex


def t_ldr_pc(instr):
    rd = (instr >> 8) & 7
    off = (instr & 0xFF) * 4

    def ex(cpu):
        cpu.r[rd] = cpu.bus.read32((cpu.r[15] & ~3) + off)
    return ex


def t_ldst_reg(instr):
    load = instr & (1 << 11)
    byte = instr & (1 << 10)
    ro = (instr >> 6) & 7
    rb = (instr >> 3) & 7
    rd = instr & 7

    def ex(cpu):
        r = cpu.r
        bus = cpu.bus
        addr = (r[rb] + r[ro]) & M32
        if load:
            if byte:
                r[rd] = bus.read8(addr)
            else:
                val = bus.read32(addr & ~3)
                r[rd] = ror(val, (addr & 3) * 8) if addr & 3 else val
        elif byte:
            bus.write8(addr, r[rd] & 0xFF)
        else:
            bus.write32(addr & ~3, r[rd])
    return ex


def t_ldst_sign(instr):
    op = (instr >> 10) & 3   # 0 STRH, 1 LDSB, 2 LDRH, 3 LDSH
    ro = (instr >> 6) & 7
    rb = (instr >> 3) & 7
    rd = instr & 7

    def ex(cpu):
        r = cpu.r
        bus = cpu.bus
        addr = (r[rb] + r[ro]) & M32
        if op == 0:
            bus.write16(addr & ~1, r[rd] & 0xFFFF)
        elif op == 1:
            r[rd] = sext(bus.read8(addr), 8) & M32
        elif op == 2:
            r[rd] = bus.read16(addr & ~1)
        else:
            r[rd] = sext(bus.read16(addr & ~1), 16) & M32
    return ex


def t_ldst_imm(instr):
    byte = instr & (1 << 12)
    load = instr & (1 << 11)
    off = (instr >> 6) & 0x1F
    if not byte:
        off *= 4
    rb = (instr >> 3) & 7
    rd = instr & 7

    def ex(cpu):
        r = cpu.r
        bus = cpu.bus
        addr = (r[rb] + off) & M32
        if load:
            if byte:
                r[rd] = bus.read8(addr)
            else:
                val = bus.read32(addr & ~3)
                r[rd] = ror(val, (addr & 3) * 8) if addr & 3 else val
        elif byte:
            bus.write8(addr, r[rd] & 0xFF)
        else:
            bus.write32(addr & ~3, r[rd])
    return ex


def t_ldst_half(instr):
    load = instr & (1 << 11)
    off = ((instr >> 6) & 0x1F) * 2
    rb = (instr >> 3) & 7
    rd = instr & 7

    def ex(cpu):
        r = cpu.r
        addr = (r[rb] + off) & M32
        if load:
            r[rd] = cpu.bus.read16(addr & ~1)
        else:
            cpu.bus.write16(addr & ~1, r[rd] & 0xFFFF)
    return ex


def t_ldst_sp(instr):
    load = instr & (1 << 11)
    rd = (instr >> 8) & 7
    off = (instr & 0xFF) * 4

    def ex(cpu):
        r = cpu.r
        addr = (r[13] + off) & M32
        if load:
            val = cpu.bus.read32(addr & ~3)
            r[rd] = ror(val, (addr & 3) * 8) if addr & 3 else val
        else:
            cpu.bus.write32(addr & ~3, r[rd])
    return ex


def t_add_pc_sp(instr):
    use_sp = instr & (1 << 11)
    rd = (instr >> 8) & 7
    off = (instr & 0xFF) * 4

    def ex(cpu):
        r = cpu.r
        base = r[13] if use_sp else (r[15] & ~3)
        r[rd] = (base + off) & M32
    return ex


def t_adjust_sp(instr):
    off = (instr & 0x7F) * 4
    if instr & 0x80:
        off = -off

    def ex(cpu):
        cpu.r[13] = (cpu.r[13] + off) & M32
    return ex


def t_push_pop(instr):
    load = instr & (1 << 11)
    extra = instr & (1 << 8)   # PUSH: LR, POP: PC
    regs = [n for n in range(8) if instr & (1 << n)]
    if load:
        def ex_pop(cpu):
            r = cpu.r
            addr = r[13]
            for n in regs:
                r[n] = cpu.bus.read32(addr)
                addr += 4
            if extra:
                pc = cpu.bus.read32(addr)
                addr += 4
                cpu.branch(pc)  # in ARMv4T POP {pc} does not switch state
            r[13] = addr & M32
        return ex_pop
    regs_out = regs + ([14] if extra else [])

    def ex_push(cpu):
        r = cpu.r
        addr = (r[13] - 4 * len(regs_out)) & M32
        r[13] = addr
        for n in regs_out:
            cpu.bus.write32(addr, r[n])
            addr += 4
    return ex_push


def t_ldm_stm(instr):
    load = instr & (1 << 11)
    rb = (instr >> 8) & 7
    regs = [n for n in range(8) if instr & (1 << n)]

    def ex(cpu):
        r = cpu.r
        addr = r[rb]
        if load:
            end = (addr + 4 * len(regs)) & M32
            for n in regs:
                r[n] = cpu.bus.read32(addr & ~3)
                addr += 4
            if rb not in regs:
                r[rb] = end
        else:
            for n in regs:
                cpu.bus.write32(addr & ~3, r[n])
                addr += 4
            r[rb] = addr & M32
    return ex


def t_bcond(instr):
    cond = (instr >> 8) & 0xF
    off = sext(instr & 0xFF, 8) * 2

    def ex(cpu):
        if cpu.check(cond):
            cpu.next_pc = (cpu.r[15] + off) & M32
    return ex


def t_swi(instr):
    def ex(cpu):
        cpu.exception(VEC_SWI, SVC, cpu.next_pc)
    return ex


def t_b(instr):
    off = sext(instr & 0x7FF, 11) * 2

    def ex(cpu):
        cpu.next_pc = (cpu.r[15] + off) & M32
    return ex


def t_bl_hi(instr):
    off = sext(instr & 0x7FF, 11) << 12

    def ex(cpu):
        cpu.r[14] = (cpu.r[15] + off) & M32
    return ex


def t_bl_lo(instr):
    off = (instr & 0x7FF) << 1

    def ex(cpu):
        r = cpu.r
        target = (r[14] + off) & M32
        r[14] = cpu.next_pc | 1
        cpu.next_pc = target & ~1
    return ex
