"""Display controller on chip select 1 (0x50000000).

8080 interface, 16 bits:  +0 - index register (command number),
                          +2 - data (register value or pixels).

The register set is like Samsung S6D0110 / Renesas HD667xx controllers:
    R00  device code (read). The firmware knows 0x8309 (width 220) and 0x8306 (224)
    R03  entry mode: bits 5:4 - direction (I/D), bit 3 - AM (1 = vertical first)
    R21  GRAM address: high byte - row (Y), low byte - column (X)
    R22  pixel write (RGB565)
    R44  horizontal window: high byte - end, low byte - start
    R45  vertical window:   high byte - end, low byte - start

The 176x220 panel itself is portrait, while the player shows a 220x176 picture:
the firmware writes with AM=1, so a picture row goes into a panel column.
"""

import struct
from array import array
import zlib

from .bus import Region

PANEL_W, PANEL_H = 176, 220


class LCD(Region):
    name = 'lcd'
    # Writes have no effect the CPU can see right away: a native CPU core may
    # queue them and hand them over in bulk (kpex/native).
    fast_writes = True
    DEVICE_CODE = 0x8309

    def __init__(self, machine):
        self.machine = machine
        self.index = 0
        self.regs = {0x03: 0x0030}
        self.x = self.y = 0
        self.win = [0, PANEL_W - 1, 0, PANEL_H - 1]   # x0, x1, y0, y1
        self.fb = [0] * (PANEL_W * PANEL_H)           # RGB565, index = y * PANEL_W + x
        self.frame = 0       # number of completed pixel write bursts
        self.pixels_pending = 0
        # Called after every completed pixel write burst: on_frame(lcd).
        # This catches every screen update without polling.
        self.on_frame = None

    # --- registers --------------------------------------------------------------

    def write_index(self, val):
        if self.index == 0x22 and self.pixels_pending:
            self.end_of_frame()
        self.index = val & 0xFF

    def end_of_frame(self):
        self.pixels_pending = 0
        self.frame += 1
        if self.on_frame:
            self.on_frame(self)

    def write_data(self, val):
        idx = self.index
        if idx == 0x22:
            self.put_pixel(val)
            return
        self.regs[idx] = val
        if idx == 0x21:
            self.x, self.y = val & 0xFF, (val >> 8) & 0xFF
        elif idx == 0x44:
            self.win[0], self.win[1] = val & 0xFF, (val >> 8) & 0xFF
            self.x = self.win[0] if self.inc_x else self.win[1]
        elif idx == 0x45:
            self.win[2], self.win[3] = val & 0xFF, (val >> 8) & 0xFF
            self.y = self.win[2] if self.inc_y else self.win[3]

    def read_data(self):
        if self.index == 0x00:
            return self.DEVICE_CODE
        if self.index == 0x22:
            return self.fb[(self.y % PANEL_H) * PANEL_W + (self.x % PANEL_W)]
        return self.regs.get(self.index, 0)

    @property
    def inc_x(self):
        return bool(self.regs.get(0x03, 0x30) & 0x10)

    @property
    def inc_y(self):
        return bool(self.regs.get(0x03, 0x30) & 0x20)

    # --- video memory -----------------------------------------------------------

    def put_pixel(self, val):
        x, y = self.x, self.y
        if x < PANEL_W and y < PANEL_H:
            self.fb[y * PANEL_W + x] = val
        self.pixels_pending += 1
        x0, x1, y0, y1 = self.win
        entry = self.regs.get(0x03, 0x30)
        dx = 1 if entry & 0x10 else -1
        dy = 1 if entry & 0x20 else -1
        if entry & 0x08:      # AM = 1: move vertically first
            y += dy
            if y > y1 or y < y0:
                y = y0 if dy > 0 else y1
                x += dx
                if x > x1 or x < x0:
                    x = x0 if dx > 0 else x1
        else:                 # AM = 0: horizontally first
            x += dx
            if x > x1 or x < x0:
                x = x0 if dx > 0 else x1
                y += dy
                if y > y1 or y < y0:
                    y = y0 if dy > 0 else y1
        self.x, self.y = x, y

    def landscape_565(self):
        """The 220x176 picture in RGB565 (as in video memory), for the window."""
        fb = self.fb
        out = array('H', bytes(PANEL_H * PANEL_W * 2))
        i = 0
        for ly in range(PANEL_W):
            for lx in range(PANEL_H):
                out[i] = fb[lx * PANEL_W + ly]
                i += 1
        return out

    def landscape_rgb(self):
        """The 220x176 picture as the user sees it: RGB bytes, row by row."""
        out = bytearray(PANEL_H * PANEL_W * 3)
        fb = self.fb
        i = 0
        for ly in range(PANEL_W):          # picture row = panel column
            for lx in range(PANEL_H):
                p = fb[lx * PANEL_W + ly]
                r, g, b = (p >> 11) & 0x1F, (p >> 5) & 0x3F, p & 0x1F
                out[i] = (r << 3) | (r >> 2)
                out[i + 1] = (g << 2) | (g >> 4)
                out[i + 2] = (b << 3) | (b >> 2)
                i += 3
        return bytes(out), PANEL_H, PANEL_W

    def save_png(self, path):
        rgb, w, h = self.landscape_rgb()
        raw = b''.join(b'\x00' + rgb[y * w * 3:(y + 1) * w * 3] for y in range(h))

        def chunk(tag, data):
            body = tag + data
            return struct.pack('>I', len(data)) + body + struct.pack('>I', zlib.crc32(body))
        png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)) +
               chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))
        with open(path, 'wb') as f:
            f.write(png)

    # --- bus ------------------------------------------------------------------

    def write16(self, off, val):
        if off & 2:
            self.write_data(val & 0xFFFF)
        else:
            self.write_index(val)

    def read16(self, off):
        return self.read_data() if off & 2 else 0

    def write32(self, off, val):
        # a 32-bit write on a 16-bit bus: two halves in turn
        self.write16(off, val & 0xFFFF)
        self.write16(off + 2, val >> 16)

    def read32(self, off):
        return self.read16(off) | (self.read16(off + 2) << 16)

    def write8(self, off, val):
        self.write16(off & ~1, val)

    def read8(self, off):
        return self.read16(off & ~1) & 0xFF
