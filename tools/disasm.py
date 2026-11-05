#!/usr/bin/env python3
"""Disassemble the firmware at an address in the player's memory (via arm-none-eabi-objdump).

    python3 tools/disasm.py gmp-m6.rom 0x2001f580 40        # ARM
    python3 tools/disasm.py gmp-m6.rom 0x2001f580 40 -t     # Thumb
"""

import argparse
import os
import subprocess
import sys
import tempfile

# Where each piece of the file ends up in memory (see the startup code at 0x70000084).
SECTIONS = [
    # (memory address, file offset, size)
    (0x20010000, 0x724, 0xEF070),     # main code and data
    (0x00000000, 0xEF794, 0x21C),     # vectors and handlers in internal SRAM
    (0x20000800, 0xFEFE0, 0x24),
    (0x70000000, 0, 0x5DD600),        # the whole file as is
]


def file_offset(addr):
    for base, off, size in SECTIONS:
        if base <= addr < base + size:
            return off + (addr - base)
    sys.exit(f'address {addr:#x} is not in any known section')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('rom')
    ap.add_argument('addr', type=lambda s: int(s, 0))
    ap.add_argument('count', type=int, nargs='?', default=32, help='number of instructions')
    ap.add_argument('-t', '--thumb', action='store_true')
    args = ap.parse_args()

    width = 2 if args.thumb else 4
    off = file_offset(args.addr)
    data = open(args.rom, 'rb').read()[off:off + args.count * width]
    with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as f:
        f.write(data)
    try:
        cmd = ['arm-none-eabi-objdump', '-D', '-b', 'binary', '-marm',
               f'--adjust-vma={args.addr:#x}']
        if args.thumb:
            cmd.append('-Mforce-thumb')
        out = subprocess.run(cmd + [f.name], capture_output=True, text=True).stdout
    finally:
        os.unlink(f.name)
    lines = out.splitlines()
    start = next(i for i, l in enumerate(lines) if l.endswith('<.data>:')) + 1
    print('\n'.join(lines[start:]))


if __name__ == '__main__':
    main()
