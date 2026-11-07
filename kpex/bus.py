"""Memory bus: finds which device serves an address.

The address space (4 GB) is split into 1 MB pages. Each page points to the
device (`Region`) responsible for it. The device gets the offset from its start.

The host running the emulator is assumed to be little-endian (x86, ARM), like
the player itself: then words in memory can be read directly.
"""

from array import array

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
    wrap around to the start, as often happens on real hardware.

    The memory is an array of 32-bit words: PyPy compiles indexing an
    array('I') into a plain load, while a memoryview.cast('I') is ~35 times
    slower. 16- and 8-bit accesses work on the containing word. The array
    never moves, so a native CPU core (Unicorn) can share it by address."""

    def __init__(self, name, size, readonly=False):
        assert size & (size - 1) == 0, 'size must be a power of two'
        self.name = name
        self.size = size
        self.mask = size - 1
        self.readonly = readonly
        self.m32 = array('I', bytes(size))
        assert self.m32.itemsize == 4

    @property
    def address(self):
        """Host address of the memory (for the native CPU core)."""
        return self.m32.buffer_info()[0]

    @property
    def data(self):
        """The memory as bytes (slow; for loading and inspection)."""
        return memoryview(self.m32).cast('B')

    def load(self, offset, blob):
        self.data[offset:offset + len(blob)] = blob

    def read32(self, off):
        return self.m32[(off & self.mask) >> 2]

    def read16(self, off):
        off &= self.mask
        return (self.m32[off >> 2] >> ((off & 2) << 3)) & 0xFFFF

    def read8(self, off):
        off &= self.mask
        return (self.m32[off >> 2] >> ((off & 3) << 3)) & 0xFF

    def write32(self, off, val):
        if not self.readonly:
            self.m32[(off & self.mask) >> 2] = val

    def write16(self, off, val):
        if not self.readonly:
            off &= self.mask
            i, shift = off >> 2, (off & 2) << 3
            m = self.m32
            m[i] = (m[i] & ~(0xFFFF << shift) & 0xFFFFFFFF) | ((val & 0xFFFF) << shift)

    def write8(self, off, val):
        if not self.readonly:
            off &= self.mask
            i, shift = off >> 2, (off & 3) << 3
            m = self.m32
            m[i] = (m[i] & ~(0xFF << shift) & 0xFFFFFFFF) | ((val & 0xFF) << shift)


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
