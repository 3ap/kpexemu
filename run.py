#!/usr/bin/env python3
"""Run the emulator without a window, for debugging.

    python3 run.py gmp-m6.rom --steps 1000000
    python3 run.py gmp-m6.rom --steps 50000 --trace trace.txt
"""

import argparse
import time
from collections import Counter

from kpex.machine import Machine


def main():
    ap = argparse.ArgumentParser(description='Kingston K-PEX emulator (no window)')
    ap.add_argument('rom')
    ap.add_argument('--steps', type=lambda s: int(float(s)), default=1_000_000,
                    help='number of instructions to run')
    ap.add_argument('--trace', help='write the address of every instruction to a file')
    ap.add_argument('--profile', action='store_true',
                    help='at the end, show the addresses where the CPU spends most time')
    ap.add_argument('--screenshot', help='at the end, save the player screen to a PNG')
    ap.add_argument('--core', choices=('python', 'unicorn'), default='python',
                    help='CPU core: our Python interpreter or Unicorn Engine (JIT)')
    args = ap.parse_args()

    m = Machine(args.rom, core=args.core)
    info = m.rom_info
    print(f'Firmware {info["version"]}, parts: {len(info["parts"])}')
    for p in info['parts']:
        print(f'  offset {p["offset"]:#09x}, size {p["size"]:#08x}')

    start = time.time()
    if args.trace:
        with open(args.trace, 'w') as f:
            for _ in range(args.steps):
                cpu = m.cpu
                f.write(f'{cpu.next_pc:08x} {"T" if cpu.t else "A"}\n')
                m.run(1, slice_size=1)
    elif args.profile:
        hist = Counter()
        try:
            for _ in range(args.steps // 97):
                m.run(97, slice_size=97)   # odd step so it does not line up with loops
                hist[m.cpu.next_pc] += 1
        except KeyboardInterrupt:
            print('stopped')
        total = sum(hist.values())
        print('Hot addresses:')
        for pc, n in hist.most_common(20):
            print(f'  {pc:08x}  {100 * n / total:5.1f}%')
    else:
        try:
            m.run(args.steps)
        except KeyboardInterrupt:
            print('stopped')
    elapsed = time.time() - start
    if args.screenshot:
        m.lcd.save_png(args.screenshot)
        print(f'screen saved to {args.screenshot}')
    print(m.cpu.dump())
    print(f'ran {m.cpu.cycles} instructions in {elapsed:.1f} s '
          f'({m.cpu.cycles / max(elapsed, 1e-9) / 1e6:.2f} MIPS)')


if __name__ == '__main__':
    main()
