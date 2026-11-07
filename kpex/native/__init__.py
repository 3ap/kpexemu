"""Native helpers for the Unicorn core (see fastio.c).

The library is built on first use with the system C compiler and loaded
through ctypes, so it works the same under PyPy and CPython.
"""

import ctypes
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, 'fastio.c')
LIBRARY = os.path.join(HERE, 'fastio.dll' if sys.platform == 'win32' else 'libfastio.so')

READ_FN = ctypes.CFUNCTYPE(ctypes.c_uint64, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_uint, ctypes.c_void_p)
WRITE_FN = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_uint, ctypes.c_uint64, ctypes.c_void_p)
FLUSH_FN = ctypes.CFUNCTYPE(None)


class WLog(ctypes.Structure):
    _fields_ = [('entries', ctypes.POINTER(ctypes.c_uint32)),
                ('len', ctypes.c_uint32), ('cap', ctypes.c_uint32),
                ('flush', FLUSH_FN), ('slow_read', READ_FN)]


class RStream(ctypes.Structure):
    _fields_ = [('data', ctypes.c_void_p),
                ('pos', ctypes.c_uint32), ('end', ctypes.c_uint32),
                ('port_mask', ctypes.c_uint32), ('port', ctypes.c_uint32),
                ('slow_read', READ_FN), ('slow_write', WRITE_FN)]


def _build():
    if os.path.exists(LIBRARY) and os.path.getmtime(LIBRARY) >= os.path.getmtime(SOURCE):
        return
    cc = os.environ.get('CC', 'cc')
    subprocess.check_call([cc, '-O2', '-shared', '-fPIC', '-o', LIBRARY, SOURCE])


_lib = None


def lib():
    global _lib
    if _lib is None:
        _build()
        _lib = ctypes.CDLL(LIBRARY)
    return _lib


def fn_addr(name):
    return ctypes.cast(getattr(lib(), name), ctypes.c_void_p).value


def _slow_read(region):
    r8, r16, r32 = region.read8, region.read16, region.read32

    def read(uc, off, size, ud):
        if size == 4:
            return r32(off)
        if size == 2:
            return r16(off)
        return r8(off)
    return READ_FN(read)


class WriteLog:
    """Writes to `region` are queued in C and replayed into it in bulk."""

    def __init__(self, region, capacity=1 << 16):
        self.region = region
        self.buf = (ctypes.c_uint32 * (2 * capacity))()
        self.view = memoryview(self.buf).cast('B').cast('I')
        self._flush_cb = FLUSH_FN(self.flush)
        self._read_cb = _slow_read(region)
        self.st = WLog(ctypes.cast(self.buf, ctypes.POINTER(ctypes.c_uint32)), 0, capacity,
                       self._flush_cb, self._read_cb)

    def flush(self):
        st = self.st
        n = st.len
        if not n:
            return
        st.len = 0
        e = self.view
        reg = self.region
        w8, w16, w32 = reg.write8, reg.write16, reg.write32
        for i in range(0, 2 * n, 2):
            key = e[i]
            size, off = key >> 24, key & 0xFFFFFF
            if size == 2:
                w16(off, e[i + 1])
            elif size == 4:
                w32(off, e[i + 1])
            else:
                w8(off, e[i + 1])

    def handlers(self):
        return fn_addr('wlog_read'), fn_addr('wlog_write'), ctypes.addressof(self.st)


class ReadStream:
    """16-bit reads of a data port are served by C from a buffer the device
    fills with `load(data)`. The device keeps its read position in `pos`."""

    def __init__(self, region, port_mask, port, slow_write):
        self.region = region
        self._data = None
        self._read_cb = _slow_read(region)
        self._write_cb = WRITE_FN(slow_write)
        self.st = RStream(None, 0, 0, port_mask, port, self._read_cb, self._write_cb)

    def load(self, data):
        """Hands `data` to C; returns the buffer the device should index."""
        buf = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
        self._data = buf
        st = self.st
        st.data = ctypes.addressof(buf)
        st.pos = 0
        st.end = len(data)
        return buf

    @property
    def pos(self):
        return self.st.pos

    @pos.setter
    def pos(self, val):
        self.st.pos = val

    def handlers(self):
        return fn_addr('rstream_read'), fn_addr('rstream_write'), ctypes.addressof(self.st)
