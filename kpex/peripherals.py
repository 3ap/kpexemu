"""Peripherals of the Telechips TCC767 CPU.

Register addresses come from the Rockbox header for its close relative TCC77x
(docs/rockbox-tcc77x.h) and are refined by watching the firmware.

Each block derives from `RegisterBlock`. A register without special logic
behaves like a memory cell: what was written is read back. The first access
to every "unknown" register is logged, which shows what peripherals the
firmware touches and what is left to implement.
"""

from .bus import Region


class RegisterBlock(Region):
    name = 'regs'
    # Offset -> register name. Known registers are not logged as "unknown".
    names = {}

    def __init__(self, machine):
        self.machine = machine
        self.values = {}
        self.seen = set()

    def log(self, msg):
        self.machine.log(f'[{self.name}] {msg}')

    def note(self, kind, off, val=None, read=False):
        """Report the first read or write of each register."""
        key = (kind, off)
        if key in self.seen:
            return
        self.seen.add(key)
        reg = self.names.get(off, '???')
        pc = self.machine.cpu.next_pc
        if read:
            self.log(f'read   +{off:#06x} {reg:<10} = {val:#010x}  pc~{pc:#010x}')
        else:
            self.log(f'write  +{off:#06x} {reg:<10} <- {val:#010x}  pc~{pc:#010x}')

    # Subclasses override read_reg / write_reg, returning NotImplemented
    # for registers that should behave like plain memory.
    def read_reg(self, off):
        return NotImplemented

    def write_reg(self, off, val):
        return NotImplemented

    def read32(self, off):
        val = self.read_reg(off)
        if val is NotImplemented:
            val = self.values.get(off, 0)
        self.note('r', off, val, read=True)
        return val

    def write32(self, off, val):
        if self.write_reg(off, val) is NotImplemented:
            self.note('w', off, val)
            self.values[off] = val


# ---------------------------------------------------------------------------

IRQ_NAMES = {0: 'EXT0', 1: 'EXT1', 2: 'EXT2', 3: 'EXT3', 4: 'I2SR', 5: 'I2ST',
             6: 'TIMER0', 8: 'USBD', 10: 'USBH', 16: 'ADC', 26: 'USBDMA', 27: 'ECC'}


class InterruptController:
    """Interrupt controller (0x80000100). Sources set bits in `pending`;
    if a bit is enabled in IEN, the CPU gets an IRQ or FIQ."""

    IEN, CREQ, IREQ, IRQSEL, ICFG, MREQ, TSTREQ, IRQ, FIQ, MIRQ, MFIQ = (
        0x00, 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18, 0x20, 0x24, 0x28, 0x2C)

    def __init__(self, cpu):
        self.cpu = cpu
        self.ien = 0
        self.pending = 0
        self.irqsel = 0     # 1 = source goes to IRQ, 0 = to FIQ
        self.icfg = 0

    def raise_irq(self, bit):
        self.pending |= 1 << bit
        self.update()

    def clear_irq(self, bit):
        self.pending &= ~(1 << bit)
        self.update()

    def update(self):
        active = self.pending & self.ien
        self.cpu.irq_line = bool(active & self.irqsel)
        self.cpu.fiq_line = bool(active & ~self.irqsel)

    def read(self, off):
        active = self.pending & self.ien
        if off == self.IEN: return self.ien
        if off == self.IREQ: return self.pending
        if off == self.IRQSEL: return self.irqsel
        if off == self.ICFG: return self.icfg
        if off == self.MREQ: return active
        if off in (self.IRQ, self.MIRQ): return active & self.irqsel
        if off in (self.FIQ, self.MFIQ): return active & ~self.irqsel & 0xFFFFFFFF
        return NotImplemented

    def write(self, off, val):
        if off == self.IEN: self.ien = val
        elif off == self.CREQ: self.pending &= ~val
        elif off == self.IRQSEL: self.irqsel = val
        elif off == self.ICFG: self.icfg = val
        elif off == self.TSTREQ: self.pending |= val
        else: return NotImplemented
        self.update()


class Timers:
    """Timers (0x80000200). Six 16/20-bit timers with compare and one
    32-bit counter TC32. All timers raise the shared TIMER0 interrupt (bit 6)
    and their own bit in TIREQ.

    The exact timer frequency depends on divider settings we do not model
    yet: we assume every timer ticks once per `TICK_CYCLES` CPU cycles
    divided by the selected prescaler."""

    TICK_CYCLES = 1

    def __init__(self, machine):
        self.machine = machine
        self.cfg = [0] * 6
        self.cnt = [0] * 6
        self.ref = [0xFFFF] * 6
        self.mref = [0] * 6
        self.tireq = 0
        self.tc32_en = 0
        self.tc32_ldv = 0
        self.tc32_cnt = 0
        self.frac = [0.0] * 6

    # TCFG: bit 0 - enabled, bit 3 - interrupt enable, bits 6:4 - divider
    DIVIDERS = [2, 4, 8, 16, 32, 1024, 4096, 1]

    def tick(self, cycles):
        for n in range(6):
            cfg = self.cfg[n]
            if not cfg & 1:
                continue
            div = self.DIVIDERS[(cfg >> 4) & 7] * self.TICK_CYCLES
            self.frac[n] += cycles / div
            steps = int(self.frac[n])
            if not steps:
                continue
            self.frac[n] -= steps
            ref = self.ref[n] or 1
            cnt = self.cnt[n] + steps
            if cnt >= ref:
                cnt %= ref
                self.tireq |= (1 << n) | (1 << (n + 8))
                if cfg & (1 << 3):
                    self.machine.intc.raise_irq(6)
            self.cnt[n] = cnt
        if self.tc32_en & (1 << 24):
            self.tc32_cnt = (self.tc32_cnt + cycles) & 0xFFFFFFFF

    def read(self, off):
        if off < 0x60:
            n, reg = off // 0x10, off % 0x10
            if reg == 0x0: return self.cfg[n]
            if reg == 0x4: return self.cnt[n]
            if reg == 0x8: return self.ref[n]
            if reg == 0xC: return self.mref[n]
        if off == 0x60: return self.tireq
        if off == 0x80: return self.tc32_en
        if off == 0x84: return self.tc32_ldv
        if off in (0x90, 0x94): return self.tc32_cnt
        return NotImplemented

    def write(self, off, val):
        if off < 0x60:
            n, reg = off // 0x10, off % 0x10
            if reg == 0x0:
                self.cfg[n] = val
                if val & (1 << 8):  # counter reset
                    self.cnt[n] = 0
            elif reg == 0x4: self.cnt[n] = val
            elif reg == 0x8: self.ref[n] = val
            elif reg == 0xC: self.mref[n] = val
            return None
        if off == 0x60:
            self.tireq &= ~val  # writing ones clears the flags
            return None
        if off == 0x80:
            self.tc32_en = val
            return None
        if off == 0x84:
            self.tc32_ldv = val
            self.tc32_cnt = val
            return None
        return NotImplemented


class ADC:
    """10-bit ADC (0x80000A00). A conversion starts by writing the channel
    number to ADCCON (+0x00) or ADCCONA (+0x80); the result is read from
    ADCSTATUS (+0x84): bits 9:0 - value, bits 26:24 non-zero = ready.

    What is on the channels (from the firmware code):
      0 - buttons via a resistor ladder (1023 = nothing pressed)
      2 - battery voltage (below 580 = battery low, power off)
      4 - above 500 = charger / USB connected (guess)
    """

    def __init__(self):
        self.channels = {0: 1023, 2: 0x2C0, 4: 0}
        self.current = 0
        self.regs = {}

    def read(self, off):
        if off == 0x84:
            return (1 << 24) | (self.channels.get(self.current, 0) & 0x3FF)
        if off == 0x04:
            return self.channels.get(self.current, 0) & 0x3FF
        return self.regs.get(off, 0)

    def write(self, off, val):
        self.regs[off] = val
        if off in (0x00, 0x80):
            self.current = val & 7


class GPIO:
    """GPIO ports A..E (0x80000300, 0x10 per port): +0 data, +4 direction
    (1 = output), +8 alternate function, +C something else.
    Reading data gives the written value for outputs, the outside level (`inputs`) for inputs.

    What is known about the inputs (from the firmware code):
      GPIOA bit 15 - power/Play button: 0 = pressed (holding ~1 s turns the player off)
      GPIOD bit 15 - HOLD switch: 0 = buttons locked
    """

    HOLD_BIT = 1 << 15
    POWER_BIT = 1 << 15

    def __init__(self):
        self.out = [0] * 5
        self.dir = [0] * 5
        self.regs = {}
        self.inputs = [0] * 5
        self.inputs[3] |= self.HOLD_BIT   # HOLD off
        self.inputs[0] |= self.POWER_BIT  # power button released
        # Called when the firmware cuts the power: on_power_off(gpio, reason).
        # Both power-off routines found switch GPIOA bit 12 from output to input
        # (GPIOA_DIR = 0xFFFF27FF instead of 0xFFFF37FF) and loop forever.
        self.on_power_off = None
        self.on_change = None   # debugging: on_change(port, reg, old, new)

    def set_power_button(self, pressed):
        if pressed:
            self.inputs[0] &= ~self.POWER_BIT
        else:
            self.inputs[0] |= self.POWER_BIT

    def set_hold(self, locked):
        if locked:
            self.inputs[3] &= ~self.HOLD_BIT
        else:
            self.inputs[3] |= self.HOLD_BIT

    def read(self, off):
        port, reg = off >> 4, off & 0xF
        if port >= 5:
            return self.regs.get(off, 0)
        if reg == 0:
            d = self.dir[port]
            return (self.out[port] & d) | (self.inputs[port] & ~d & 0xFFFFFFFF)
        if reg == 4:
            return self.dir[port]
        return self.regs.get(off, 0)

    POWER_HOLD_DIR_BIT = 1 << 12

    def write(self, off, val):
        port, reg = off >> 4, off & 0xF
        if port < 5 and reg == 0:
            old = self.out[port]
            self.out[port] = val
            if old != val and self.on_change:
                self.on_change(port, 'out', old, val)
        elif port < 5 and reg == 4:
            old = self.dir[port]
            self.dir[port] = val
            if old != val and self.on_change:
                self.on_change(port, 'dir', old, val)
            if (port == 0 and old & self.POWER_HOLD_DIR_BIT and not val & self.POWER_HOLD_DIR_BIT
                    and self.on_power_off):
                self.on_power_off(self, f'GPIOA_DIR {old:#010x} -> {val:#010x}')
        else:
            self.regs[off] = val


class OHCI:
    """OHCI USB host (0x80000D00). Emulated as a controller with no devices
    attached: reset completes instantly, the port is empty."""

    NAMES = {0x00: 'HcRevision', 0x04: 'HcControl', 0x08: 'HcCommandStatus',
             0x0C: 'HcInterruptStatus', 0x10: 'HcInterruptEnable', 0x14: 'HcInterruptDisable',
             0x18: 'HcHCCA', 0x34: 'HcFmInterval', 0x3C: 'HcFmNumber',
             0x48: 'HcRhDescriptorA', 0x4C: 'HcRhDescriptorB', 0x50: 'HcRhStatus',
             0x54: 'HcRhPortStatus1'}

    def __init__(self):
        self.regs = {0x00: 0x10, 0x34: 0x2EDF, 0x48: 0x01000001}
        self.int_enable = 0

    def read(self, off):
        if off == 0x08:
            return self.regs.get(0x08, 0) & ~1   # HCR: reset has already finished
        if off == 0x10 or off == 0x14:
            return self.int_enable
        if off == 0x50:
            return 0
        if off == 0x54:
            return 0x100   # port powered (PPS), no device connected (CCS = 0)
        return self.regs.get(off, 0)

    def write(self, off, val):
        if off == 0x08:
            if val & 1:
                self.regs[0x04] = 0     # reset: HcControl -> UsbReset
            if val & 8:
                self.regs[0x04] = self.regs.get(0x04, 0) & ~0x100  # ownership change (IR)
            self.regs[0x08] = val & ~1
        elif off == 0x0C:
            self.regs[0x0C] = self.regs.get(0x0C, 0) & ~val
        elif off == 0x10:
            self.int_enable |= val
        elif off == 0x14:
            self.int_enable &= ~val
        elif off in (0x50, 0x54):
            pass  # writes to root hub registers are commands, the state does not change
        else:
            self.regs[off] = val


class SystemRegs(RegisterBlock):
    """Registers at 0x80000000..0x800FFFFF: audio interface, interrupts,
    timers, GPIO, clocks, ADC, ECC and more."""

    name = 'sys'
    names = {
        0x100: 'IEN', 0x104: 'CREQ', 0x108: 'IREQ', 0x10C: 'IRQSEL', 0x110: 'ICFG',
        0x114: 'MREQ', 0x120: 'IRQ', 0x124: 'FIQ',
        0x300: 'GPIOA', 0x304: 'GPIOA_DIR', 0x308: 'GPIOA_FN',
        0x310: 'GPIOB', 0x314: 'GPIOB_DIR', 0x318: 'GPIOB_FN',
        0x320: 'GPIOC', 0x324: 'GPIOC_DIR', 0x328: 'GPIOC_FN',
        0x330: 'GPIOD', 0x334: 'GPIOD_DIR', 0x338: 'GPIOD_FN',
        0x340: 'GPIOE', 0x344: 'GPIOE_DIR', 0x348: 'GPIOE_FN',
        0x364: 'BMI',
        0x400: 'CLKCTRL', 0x404: 'PLL0CFG', 0x408: 'CLKDIVA?', 0x40C: 'CLKDIVC',
        0x410: 'MODECTR', 0x414: 'BCLKCTR', 0x418: 'SWRESET',
        0x41C: 'PCLKCFG0', 0x420: 'PCLKCFG1', 0x424: 'PCLKCFG2', 0x428: 'PCLKCFG3',
        0x42C: 'PCLKCFG4', 0x430: 'PCLKCFG5', 0x434: 'PCLKCFG6',
        0x440: 'PLL?440', 0x444: 'PLL?444',
        0xA00: 'ADCCON', 0xA04: 'ADCDATA', 0xA80: 'ADCCONA', 0xA84: 'ADCSTATUS', 0xA88: 'ADCCFG',
        **{0xD00 + k: v for k, v in OHCI.NAMES.items()},
        0x900: 'ECC_CTRL', 0xA10: 'BITLEN', 0xA14: 'BITLEN_CFG?',
    }

    PLL_LOCKED = 1 << 20
    ADC_REGS = (0xA00, 0xA04, 0xA80, 0xA84, 0xA88)

    def __init__(self, machine):
        super().__init__(machine)
        self.intc = machine.intc
        self.timers = machine.timers
        self.adc = machine.adc
        self.gpio = machine.gpio
        self.ohci = OHCI()

    def read_reg(self, off):
        if 0x100 <= off < 0x140:
            return self.intc.read(off - 0x100)
        if 0x200 <= off < 0x2A0:
            return self.timers.read(off - 0x200)
        if 0x300 <= off < 0x350:
            return self.gpio.read(off - 0x300)
        if 0xD00 <= off < 0xE00:
            return self.ohci.read(off - 0xD00)
        if off == 0x404:
            return self.values.get(off, 0) | self.PLL_LOCKED
        if off in self.ADC_REGS:
            return self.adc.read(off - 0xA00)
        if off == 0xA10:
            # Hardware "find highest bit": returns the bit length of the
            # written number (0xB1 -> 8, 0x08 -> 4). The firmware uses it as a
            # chip authenticity check: on a wrong answer it deliberately
            # corrupts an OS kernel variable and tasks stop switching.
            return self.values.get(0xA10, 0).bit_length()
        return NotImplemented

    def write_reg(self, off, val):
        if 0x100 <= off < 0x140:
            self.note('w', off, val)
            return self.intc.write(off - 0x100, val)
        if 0x200 <= off < 0x2A0:
            self.note('w', off, val)
            return self.timers.write(off - 0x200, val)
        if off in self.ADC_REGS:
            self.note('w', off, val)
            return self.adc.write(off - 0xA00, val)
        if 0x300 <= off < 0x350:
            self.note('w', off, val)
            return self.gpio.write(off - 0x300, val)
        if 0xD00 <= off < 0xE00:
            self.note('w', off, val)
            return self.ohci.write(off - 0xD00, val)
        return NotImplemented


class NandController(RegisterBlock):
    """NAND controller (0x90000000). Only a stub for now."""
    name = 'nand'
    names = {0x00: 'NFC_CMD', 0x0C: 'NFC_SADDR', 0x10: 'NFC_WDATA', 0x40: 'NFC_SDATA',
             0x50: 'NFC_CTRL', 0x60: 'NFC_IREQ', 0x64: 'NFC_RST'}


class MemoryController(RegisterBlock):
    """SDRAM and external bus controller (0xF0000000). Timing settings do
    not matter to the emulator, we just remember them."""
    name = 'memctl'
    names = {0x00: 'SDCFG', 0x04: 'SDFSM', 0x08: 'MCFG', 0x0C: 'TST',
             0x10: 'CSCFG0', 0x14: 'CSCFG1', 0x18: 'CSCFG2', 0x1C: 'CSCFG3',
             0x20: 'CLKCFG', 0x24: 'SDCMD'}


class Unknown(RegisterBlock):
    """Stub for address ranges we know nothing about yet."""

    def __init__(self, machine, name):
        super().__init__(machine)
        self.name = name
