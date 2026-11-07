"""Memory bus: finds which device serves an address.

The address space (4 GB) is split into 1 MB pages. Each page points to the
device (`Region`) responsible for it. The device gets the offset from its start.

The host running the emulator is assumed to be little-endian (x86, ARM), like
the player itself: then words in a bytearray can be read directly.
"""

import mmap

PAGE_SHIFT = 20


class Region:
    """Base class of a bus device. By default 8- and 16-bit accesses are
    expressed through 32-bit ones."""

    name = '?'

    def read32(self, off):
        return 0

    def write32(self, off, val):
        pass

    def read16(self, off):
        return (self.read32(off & ~3) >> ((off & 2) * 8)) & 0xFFFF

    def write16(self, off, val):
        shift = (off & 2) * 8
        old = self.read32(off & ~3)
        self.write32(off & ~3, (old & ~(0xFFFF << shift)) | (val << shift))

    def read8(self, off):
        return (self.read32(off & ~3) >> ((off & 3) * 8)) & 0xFF

    def write8(self, off, val):
        shift = (off & 3) * 8
        old = self.read32(off & ~3)
        self.write32(off & ~3, (old & ~(0xFF << shift)) | (val << shift))


class RAM(Region):
    """Plain memory. The size must be a power of two: addresses past the end
    wrap around to the start, as often happens on real hardware."""

    def __init__(self, name, size, readonly=False):
        assert size & (size - 1) == 0, 'size must be a power of two'
        self.name = name
        self.size = size
        self.mask = size - 1
        self.readonly = readonly
        # Anonymous mmap instead of bytearray: its memory never moves, so a
        # native CPU core (Unicorn) can work on the same bytes directly.
        self.data = mmap.mmap(-1, size)
        self._remap()

    @property
    def address(self):
        """Host address of the memory (for the native CPU core)."""
        import ctypes
        return ctypes.addressof(ctypes.c_char.from_buffer(self.data))

    def _remap(self):
        mv = memoryview(self.data)
        self.m32 = mv.cast('I')
        self.m16 = mv.cast('H')

    def load(self, offset, blob):
        self.data[offset:offset + len(blob)] = blob

    def read32(self, off):
        return self.m32[(off & self.mask) >> 2]

    def read16(self, off):
        return self.m16[(off & self.mask) >> 1]

    def read8(self, off):
        return self.data[off & self.mask]

    def write32(self, off, val):
        if not self.readonly:
            self.m32[(off & self.mask) >> 2] = val

    def write16(self, off, val):
        if not self.readonly:
            self.m16[(off & self.mask) >> 1] = val

    def write8(self, off, val):
        if not self.readonly:
            self.data[off & self.mask] = val


class Bus:
    def __init__(self, log=print):
        self.pages = [None] * (1 << (32 - PAGE_SHIFT))
        self.bases = [0] * (1 << (32 - PAGE_SHIFT))
        self.log = log
        self.unmapped_seen = set()
        self.cpu = None  # for error messages (instruction address)

    def map(self, base, size, region):
        """Attach a device to the address range [base, base + size)."""
        assert base % (1 << PAGE_SHIFT) == 0 and size % (1 << PAGE_SHIFT) == 0
        for page in range(base >> PAGE_SHIFT, (base + size) >> PAGE_SHIFT):
            self.pages[page] = region
            self.bases[page] = base

    def _unmapped(self, kind, addr, val=None):
        key = (kind, addr)
        if key not in self.unmapped_seen:
            self.unmapped_seen.add(key)
            pc = self.cpu.next_pc if self.cpu else 0
            what = f'write {val:#x}' if val is not None else 'read'
            self.log(f'[bus] {what} at unmapped address {addr:#010x} ({kind}), pc~{pc:#010x}')
        return 0

    def region_at(self, addr):
        return self.pages[addr >> PAGE_SHIFT]

    def read32(self, addr):
        page = addr >> PAGE_SHIFT
        region = self.pages[page]
        if region is None:
            return self._unmapped('32', addr)
        return region.read32(addr - self.bases[page])

    def read16(self, addr):
        page = addr >> PAGE_SHIFT
        region = self.pages[page]
        if region is None:
            return self._unmapped('16', addr)
        return region.read16(addr - self.bases[page])

    def read8(self, addr):
        page = addr >> PAGE_SHIFT
        region = self.pages[page]
        if region is None:
            return self._unmapped('8', addr)
        return region.read8(addr - self.bases[page])

    def write32(self, addr, val):
        page = addr >> PAGE_SHIFT
        region = self.pages[page]
        if region is None:
            return self._unmapped('32', addr, val)
        region.write32(addr - self.bases[page], val)

    def write16(self, addr, val):
        page = addr >> PAGE_SHIFT
        region = self.pages[page]
        if region is None:
            return self._unmapped('16', addr, val)
        region.write16(addr - self.bases[page], val)

    def write8(self, addr, val):
        page = addr >> PAGE_SHIFT
        region = self.pages[page]
        if region is None:
            return self._unmapped('8', addr, val)
        region.write8(addr - self.bases[page], val)
