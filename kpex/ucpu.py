"""CPU core on top of Unicorn Engine (QEMU's JIT), a drop-in replacement for
`cpu.CPU`.

Unicorn has no ARM940T, so we take ARM946 (ARMv5TE): it runs ARMv4T code and
has the same kind of memory protection unit (MPU) with a compatible CP15.

* RAM (SRAM, SDRAM with mirrors, ROM) is the same memory the Python devices
  see: Unicorn works on it directly, without calling back into Python.
* Everything else on the bus is mapped as MMIO: each access calls the
  device's read8/16/32 or write8/16/32 in Python.
* Unicorn knows nothing about interrupt lines, so we enter IRQ/FIQ ourselves
  between runs, the way the CPU itself does it.
"""

from unicorn import (Uc, UcError, UC_ARCH_ARM, UC_MODE_ARM, UC_PROT_ALL)
from unicorn import arm_const as A

from unicorn.unicorn_py3.unicorn import uclib

from . import native
from .bus import RAM, PAGE_SHIFT
from .cpu import M32, FIQ, IRQ, MODE_NAMES, VEC_IRQ, VEC_FIQ

PAGE = 1 << PAGE_SHIFT

REG_IDS = [A.UC_ARM_REG_R0 + n for n in range(13)] + [A.UC_ARM_REG_SP, A.UC_ARM_REG_LR, A.UC_ARM_REG_PC]


class Registers:
    """`cpu.r[n]` for code that wants to look at (or change) registers."""

    def __init__(self, uc):
        self.uc = uc

    def __getitem__(self, n):
        return self.uc.reg_read(REG_IDS[n])

    def __setitem__(self, n, val):
        self.uc.reg_write(REG_IDS[n], val & M32)


class UnicornCPU:
    def __init__(self, bus):
        self.bus = bus
        self.uc = uc = Uc(UC_ARCH_ARM, UC_MODE_ARM)
        uc.ctl_set_cpu_model(A.UC_CPU_ARM_946)
        self.r = Registers(uc)
        self.halted = False
        self.irq_line = False
        self.fiq_line = False
        self.cycles = 0
        self.on_undefined = None
        self.write_logs = []        # queued device writes, flushed after every run
        self.native_handlers = []   # keeps the C-side structures alive

    # --- memory map: copied from the bus once all devices are attached ------------

    def attach(self):
        bus = self.bus
        page = 0
        count = len(bus.pages)
        while page < count:
            region = bus.pages[page]
            if region is None:
                start = page
                while page < count and bus.pages[page] is None:
                    page += 1
                self._map_unmapped(start << PAGE_SHIFT, (page - start) << PAGE_SHIFT)
                continue
            start = page
            base = bus.bases[page]
            while page < count and bus.pages[page] is region and bus.bases[page] == base:
                page += 1
            addr, size = start << PAGE_SHIFT, (page - start) << PAGE_SHIFT
            if isinstance(region, RAM):
                # Smaller memory is repeated over the window (mirrors).
                step = min(region.size, size)
                ptr = region.address
                for a in range(addr, addr + size, step):
                    self.uc.mem_map_ptr(a, step, UC_PROT_ALL, ptr + ((a - base) & region.mask))
            else:
                self._map_mmio(addr, size, region, addr - base)

    def _map_mmio(self, addr, size, region, delta):
        r8, r16, r32 = region.read8, region.read16, region.read32
        w8, w16, w32 = region.write8, region.write16, region.write32

        def read(uc, off, sz, _):
            off += delta
            if sz == 4:
                return r32(off)
            if sz == 2:
                return r16(off)
            return r8(off)

        def write(_uc, off, sz, val, _):
            off += delta
            if sz == 4:
                w32(off, val)
            elif sz == 2:
                w16(off, val)
            else:
                w8(off, val)
            # No emu_stop() here even if an interrupt became pending: Unicorn
            # cannot tell how many instructions ran before a stop, and
            # counting the whole slice makes the timers outrun the CPU. The
            # interrupt is taken at the end of the slice instead.

        # The busiest ports are served by C code (kpex/native) without Python.
        fast = None
        if getattr(region, 'fast_writes', False):
            fast = native.WriteLog(region)
            self.write_logs.append(fast)
        elif getattr(region, 'fast_read_port', None):
            mask, port = region.fast_read_port
            fast = region.stream = native.ReadStream(region, mask, port, write)
        if fast is not None:
            assert delta == 0
            rd, wr, ud = fast.handlers()
            self.native_handlers.append(fast)
            status = uclib.uc_mmio_map(self.uc._uch, addr, size, rd, ud, wr, ud)
            if status:
                raise UcError(status)
            return
        self.uc.mmio_map(addr, size, read, None, write, None)

    def _map_unmapped(self, addr, size):
        """Addresses outside every device: report each access once and read
        zero, as the Python bus does."""
        bus = self.bus

        def read(uc, off, sz, _):
            return bus._unmapped(str(sz * 8), addr + off)

        def write(uc, off, sz, val, _):
            bus._unmapped(str(sz * 8), addr + off, val)

        self.uc.mmio_map(addr, size, read, None, write, None)

    # --- status register ---------------------------------------------------------

    @property
    def cpsr(self):
        return self.uc.reg_read(A.UC_ARM_REG_CPSR)

    def set_cpsr(self, val):
        self.uc.reg_write(A.UC_ARM_REG_CPSR, val)

    @property
    def t(self):
        return (self.cpsr >> 5) & 1

    @property
    def mode(self):
        return self.cpsr & 0x1F

    @property
    def next_pc(self):
        return self.uc.reg_read(A.UC_ARM_REG_PC)

    @next_pc.setter
    def next_pc(self, val):
        self.uc.reg_write(A.UC_ARM_REG_PC, val)

    @property
    def pc(self):
        return self.next_pc

    # --- exceptions --------------------------------------------------------------

    def _high_vectors(self):
        # CP15 c1 (control register), bit 13 = vectors at 0xFFFF0000
        ctrl = self.uc.reg_read(A.UC_ARM_REG_CP_REG, (15, 0, 0, 1, 0, 0, 0))
        return bool(ctrl & (1 << 13))

    def _interrupt(self, vector, mode):
        uc = self.uc
        cpsr = uc.reg_read(A.UC_ARM_REG_CPSR)
        lr = uc.reg_read(A.UC_ARM_REG_PC) + 4
        new = (cpsr & ~0x3F) | 0x80 | mode     # I = 1, T = 0, new mode
        if mode == FIQ:
            new |= 0x40
        uc.reg_write(A.UC_ARM_REG_CPSR, new)
        uc.reg_write(A.UC_ARM_REG_SPSR, cpsr)
        uc.reg_write(A.UC_ARM_REG_LR, lr)
        uc.reg_write(A.UC_ARM_REG_PC, (0xFFFF0000 if self._high_vectors() else 0) + vector)
        self.halted = False

    # --- main loop ---------------------------------------------------------------

    def run(self, count):
        """Run `count` instructions (Unicorn counts them exactly). Returns the number."""
        uc = self.uc
        if self.fiq_line or self.irq_line:
            # Interrupts are only checked here, between runs. If both are
            # pending, enter IRQ and then FIQ on top of it (IRQ entry leaves
            # F alone): the FIQ handler runs first and returns straight into
            # the IRQ handler, as on the real CPU. Taking only the FIQ would
            # starve the IRQ when FIQs come every run (audio).
            cpsr = uc.reg_read(A.UC_ARM_REG_CPSR)
            if self.irq_line and not cpsr & 0x80:
                self._interrupt(VEC_IRQ, IRQ)
            if self.fiq_line and not cpsr & 0x40:
                self._interrupt(VEC_FIQ, FIQ)
        if self.halted:
            return 0
        pc = uc.reg_read(A.UC_ARM_REG_PC)
        if uc.reg_read(A.UC_ARM_REG_CPSR) & 0x20:
            pc |= 1
        try:
            uc.emu_start(pc, 0xFFFFFFFF, count=count)
        except UcError as e:
            self.bus.log(f'[cpu] unicorn stopped: {e} at pc={self.next_pc:#010x}')
            self.halted = True
        for log in self.write_logs:
            log.flush()
        self.cycles += count
        return count

    def dump(self):
        r = [self.r[n] for n in range(16)]
        lines = []
        for row in range(4):
            lines.append('  '.join(f'r{n:<2}={r[n]:08x}' for n in range(row * 4, row * 4 + 4)))
        cpsr = self.cpsr
        flags = ''.join(ch if cpsr & (1 << b) else '-' for ch, b in zip('NZCVIFT', (31, 30, 29, 28, 7, 6, 5)))
        lines.append(f'pc ={self.next_pc:08x}  cpsr={cpsr:08x} [{flags} {MODE_NAMES.get(cpsr & 0x1F, "?")}]')
        return '\n'.join(lines)
