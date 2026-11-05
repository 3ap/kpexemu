"""Storage device with an ATA interface on chip select 0 (0x40000000).

The firmware talks to it like a normal IDE disk: the "task file" registers
are 4 bytes apart, data is read as 16-bit words.

    +0x00  data (16 bits)
    +0x04  error / feature
    +0x08  sector count
    +0x0C  LBA 7:0
    +0x10  LBA 15:8
    +0x14  LBA 23:16
    +0x18  device (bit 6 = LBA, bits 3:0 = LBA 27:24)
    +0x1C  status / command

The disk contents are kept in an image file (disk.img by default). The file
is created sparse, so only written sectors take space on the host disk.
"""

import os
import struct

from .bus import Region

SECTOR = 512

# Status register bits
BSY, DRDY, DF, DSC, DRQ, ERR = 0x80, 0x40, 0x20, 0x10, 0x08, 0x01


class ATADisk(Region):
    name = 'ata'

    def __init__(self, machine, path, size_mb=512):
        self.machine = machine
        self.path = path
        if not os.path.exists(path):
            with open(path, 'wb') as f:
                f.truncate(size_mb << 20)
            machine.log(f'[ata] created an empty disk image {path} ({size_mb} MB)')
        self.file = open(path, 'r+b')
        self.file.seek(0, os.SEEK_END)
        self.sectors = self.file.tell() // SECTOR

        self.error = 0
        self.feature = 0
        self.count = 0
        self.lba = [0, 0, 0]
        self.device = 0
        self.status = DRDY | DSC

        self.buffer = b''      # data to hand to the CPU (read)
        self.pos = 0
        self.write_buf = None  # data from the CPU (write)
        self.write_lba = 0
        self.write_left = 0
        self.seen = set()
        self.trace = False     # True: log every sector read/write

    def log(self, msg):
        self.machine.log(f'[ata] {msg}')

    # --- address ---------------------------------------------------------------

    def current_lba(self):
        return (((self.device & 0x0F) << 24) | (self.lba[2] << 16) |
                (self.lba[1] << 8) | self.lba[0])

    def sector_count(self):
        return self.count or 256

    # --- commands -------------------------------------------------------------

    def command(self, cmd):
        self.error = 0
        self.status = DRDY | DSC
        if self.device & 0x10:          # there is no second (slave) device
            self.status = DRDY | ERR
            self.error = 0x04
            return
        if cmd == 0xEC:                 # IDENTIFY DEVICE
            self.start_read(self.identify())
        elif cmd in (0x20, 0x21, 0xC4):  # READ SECTORS / READ MULTIPLE
            lba, n = self.current_lba(), self.sector_count()
            if self.trace:
                self.log(f'read LBA {lba} x{n}  pc~{self.machine.cpu.next_pc:#010x}')
            self.file.seek(lba * SECTOR)
            data = self.file.read(n * SECTOR)
            self.start_read(data + bytes(n * SECTOR - len(data)))
        elif cmd in (0x30, 0x31, 0xC5):  # WRITE SECTORS / WRITE MULTIPLE
            self.write_lba = self.current_lba()
            self.write_left = self.sector_count()
            if self.trace:
                self.log(f'write LBA {self.write_lba} x{self.write_left}  pc~{self.machine.cpu.next_pc:#010x}')
            self.write_buf = bytearray()
            self.status = DRDY | DSC | DRQ
        elif cmd == 0x63:
            # Vendor command: 512 bytes of device info. The firmware takes
            # bytes 1..16 from it (looks like a unique device ID).
            info = bytearray(SECTOR)
            info[1:17] = b'KPEX-EMU-0000001'
            self.start_read(bytes(info))
        else:
            # Other commands (reset, power, cache and the vendor 0x61/0x62/0x64)
            # are simply treated as successful.
            if cmd not in self.seen:
                self.seen.add(cmd)
                self.log(f'command {cmd:#04x} (accepted, no action), pc~{self.machine.cpu.next_pc:#010x}')

    def start_read(self, data):
        self.buffer = data
        self.pos = 0
        self.status = DRDY | DSC | DRQ

    def identify(self):
        words = [0] * 256
        words[0] = 0x0040                # fixed disk
        heads, spt = 16, 63
        cyls = min(self.sectors // (heads * spt), 16383)
        words[1], words[3], words[6] = cyls, heads, spt

        def text(first, count, s):
            s = s.ljust(count * 2)
            for n in range(count):
                words[first + n] = (ord(s[2 * n]) << 8) | ord(s[2 * n + 1])
        text(10, 10, 'KPEXEMU0001')
        text(23, 4, '1.0')
        text(27, 20, 'KPEX EMULATED DISK')
        words[47] = 0x8001
        words[49] = 0x0200               # LBA supported
        words[60] = self.sectors & 0xFFFF
        words[61] = self.sectors >> 16
        return struct.pack('<256H', *words)

    # --- data ---------------------------------------------------------------

    def read_data(self):
        if self.pos >= len(self.buffer):
            return 0
        val = self.buffer[self.pos] | (self.buffer[self.pos + 1] << 8)
        self.pos += 2
        if self.pos >= len(self.buffer):
            self.status = DRDY | DSC
        return val

    def write_data(self, val):
        if self.write_buf is None:
            return
        self.write_buf += bytes((val & 0xFF, val >> 8))
        if len(self.write_buf) == SECTOR:
            self.file.seek(self.write_lba * SECTOR)
            self.file.write(self.write_buf)
            self.write_lba += 1
            self.write_left -= 1
            self.write_buf = bytearray() if self.write_left else None
            if self.write_buf is None:
                self.file.flush()
                self.status = DRDY | DSC

    # --- bus -----------------------------------------------------------------

    def read_reg(self, off):
        off &= 0x1F
        if off == 0x00: return self.read_data()
        if off == 0x04: return self.error
        if off == 0x08: return self.count
        if off == 0x0C: return self.lba[0]
        if off == 0x10: return self.lba[1]
        if off == 0x14: return self.lba[2]
        if off == 0x18: return self.device
        if off == 0x1C: return self.status
        return 0

    def write_reg(self, off, val):
        off &= 0x1F
        if off == 0x00: self.write_data(val & 0xFFFF)
        elif off == 0x04: self.feature = val & 0xFF
        elif off == 0x08: self.count = val & 0xFF
        elif off == 0x0C: self.lba[0] = val & 0xFF
        elif off == 0x10: self.lba[1] = val & 0xFF
        elif off == 0x14: self.lba[2] = val & 0xFF
        elif off == 0x18: self.device = val & 0xFF
        elif off == 0x1C: self.command(val & 0xFF)

    def read32(self, off): return self.read_reg(off)
    def read16(self, off): return self.read_reg(off)
    def read8(self, off): return self.read_reg(off) & 0xFF
    def write32(self, off, val): self.write_reg(off, val)
    def write16(self, off, val): self.write_reg(off, val)
    def write8(self, off, val): self.write_reg(off, val)
