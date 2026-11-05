"""The whole Kingston K-PEX player: CPU, memory and peripherals."""

import struct

from .bus import Bus, RAM
from .cpu import CPU, SVC
from . import peripherals as P
from .ata import ATADisk
from .lcd import LCD

ROM_BASE = 0x70000000
SDRAM_BASE = 0x20000000
SDRAM_SIZE = 16 << 20      # Hynix HY57V281620: 128 Mbit = 16 MB
SDRAM_WINDOW = 256 << 20   # SDRAM address window; memory repeats in it (mirrors) since the upper address lines are not connected
SRAM_SIZE = 64 << 10       # TCC767 internal memory at address 0 (the size is a guess)

CPU_HZ = 120_000_000       # nominal clock: used to convert cycles to time


def parse_rom_header(data):
    """Firmware file header (offsets 0x04..0x50).

    0x08  "Ver:1234"
    0x14  number of parts
    0x20  16-byte entries: file offset, first sector,
          sector count (512-byte sectors), checksum
    """
    count = struct.unpack_from('<I', data, 0x14)[0]
    parts = []
    for n in range(count):
        offset, first, sectors, csum = struct.unpack_from('<4I', data, 0x20 + 16 * n)
        parts.append({'offset': offset, 'first_sector': first, 'sectors': sectors,
                      'size': sectors * 512, 'checksum': csum})
    return {'version': data[8:16].decode('ascii', 'replace'), 'parts': parts}


class Machine:
    def __init__(self, rom_path, disk_path='disk.img', log=print):
        self.log = log
        self.bus = Bus(log=log)
        self.cpu = CPU(self.bus)
        self.bus.cpu = self.cpu

        rom = open(rom_path, 'rb').read()
        self.rom_info = parse_rom_header(rom)
        size = 1 << (len(rom) - 1).bit_length()
        self.rom = RAM('rom', size)
        self.rom.load(0, rom)

        self.sram = RAM('sram', SRAM_SIZE)
        self.sdram = RAM('sdram', SDRAM_SIZE)

        self.intc = P.InterruptController(self.cpu)
        self.timers = P.Timers(self)
        self.adc = P.ADC()
        self.gpio = P.GPIO()
        self.powered_off = None   # reason, if the firmware cut the power
        self.sysregs = P.SystemRegs(self)

        bus = self.bus
        bus.map(0x00000000, 1 << 20, self.sram)
        bus.map(SDRAM_BASE, SDRAM_WINDOW, self.sdram)
        bus.map(ROM_BASE, size, self.rom)
        bus.map(0x80000000, 1 << 20, self.sysregs)
        bus.map(0x90000000, 1 << 20, P.NandController(self))
        bus.map(0xF0000000, 1 << 20, P.MemoryController(self))
        self.disk = ATADisk(self, disk_path)
        bus.map(0x40000000, 1 << 20, self.disk)
        self.lcd = LCD(self)
        bus.map(0x50000000, 1 << 20, self.lcd)
        # Areas we know nothing about yet: accesses to them show up in the log.
        for base, name in ((0x30000000, 'cs30'), (0x60000000, 'cs60')):
            bus.map(base, 1 << 20, P.Unknown(self, name))

        self.reset()

    def reset(self):
        cpu = self.cpu
        cpu.set_cpsr(0xD3)          # SVC mode, IRQ and FIQ disabled
        cpu.next_pc = ROM_BASE      # the boot loader jumps to the start of the firmware

    def _power_off(self, gpio, reason):
        cpu = self.cpu
        self.powered_off = reason
        self.log(f'[power] the firmware turned the player off: {reason}; pc~{cpu.next_pc:#010x} lr={cpu.r[14]:#010x}')
        sp = cpu.r[13]
        self.log('[power] stack: ' + ' '.join(f'{self.bus.read32(sp + 4 * k):08x}' for k in range(16)))
        cpu.halted = True   # nothing more to run: on real hardware the power goes away here

    def run(self, instructions, slice_size=2000):
        """Run about `instructions` instructions, advancing peripheral time
        between slices."""
        cpu = self.cpu
        done = 0
        while done < instructions:
            if self.powered_off:
                break
            n = cpu.run(min(slice_size, instructions - done))
            if n == 0:  # the CPU waits for an interrupt: just let time pass
                n = slice_size
                cpu.cycles += n
            self.timers.tick(n)
            done += n
        return done
