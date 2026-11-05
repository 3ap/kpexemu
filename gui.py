#!/usr/bin/env python3
"""Kingston K-PEX emulator with a window (SDL2 via cffi). Best run under PyPy:

    pypy3 gui.py gmp-m6.rom

Prepare the disk image with resources once:  python3 tools/mkdisk.py gmp-m6.rom disk.img

With --sync-folder DIR the player's files live in a normal folder: it is copied to
the disk image on start and the player's changes are copied back on exit.

Controls (see KEYS):
    arrows         - directions (left 1, right 2, up 9, down 8)
    Enter          - OK / open the selected item (code 13)
    Backspace / 7  - Back (code 12)
    M / 3          - Menu (code 5)
    Space          - Play: opens the MP3 player directly (code 0)
    1, 2, 4, 5, 6  - other buttons, purpose unknown (codes 3, 4, 7, 10, 11)
    P              - power button (GPIOA bit 15); a long press turns the player off
    H              - HOLD switch
    S              - save a screenshot to screenshot-NNN.png
    Esc            - quit
"""

import argparse
import time

from kpex import fat16, sdl
from kpex.machine import Machine
from kpex.lcd import PANEL_W, PANEL_H

# ADC channel 0 value for each button. The firmware turns it into a button code
# using thresholds (function at 0x2001ffd0): <=40 -> 0, <=110 -> 12, <=190 -> 13,
# <=270 -> 3, <=350 -> 7, <=430 -> 4, <=500 -> 5, <=590 -> 9, <=650 -> 8,
# <=700 -> 2, <=780 -> 1, <=850 -> 11, <=980 -> 10, above that nothing is pressed.
# (The pairs 8/9 and 1/2 swap when the screen is rotated, so they are directions.)
ADC_FOR_CODE = {0: 20, 12: 75, 13: 150, 3: 230, 7: 310, 4: 390, 5: 465,
                9: 545, 8: 620, 2: 675, 1: 740, 11: 815, 10: 915}
NO_KEY = 1023

KEYS = {
    sdl.K_LEFT: 1, sdl.K_RIGHT: 2, sdl.K_UP: 9, sdl.K_DOWN: 8,
    sdl.K_BACKSPACE: 12,  # Back
    ord('m'): 5,          # Menu
    sdl.K_RETURN: 13,     # OK: opens the selected menu item
    sdl.K_SPACE: 0,       # Play: opens the MP3 player directly from the menu
    ord('1'): 3, ord('2'): 4, ord('3'): 5, ord('4'): 7,  # '3' (code 5) is Menu
    ord('5'): 10, ord('6'): 11, ord('7'): 12,           # '7' (code 12) is Back
}

FRAME_TIME = 1 / 30      # how often to refresh the window
CHUNK = 100_000          # instructions to run between window checks
# The firmware polls the buttons on a timer, and the emulator is slower than the
# real player: a short key press could end before the firmware sees it.
# So a button stays pressed for at least this many instructions (~0.2 s under PyPy).
MIN_HOLD = 2_000_000


def main():
    ap = argparse.ArgumentParser(description='Kingston K-PEX emulator')
    ap.add_argument('rom')
    ap.add_argument('--disk', default='disk.img')
    ap.add_argument('--scale', type=int, default=3)
    ap.add_argument('--verbose', action='store_true', help='print the peripheral log')
    ap.add_argument('--sync-folder', metavar='DIR',
                    help='keep the player files in this folder: copied to the disk on start, back on exit')
    ap.add_argument('--max-instructions', type=lambda v: int(float(v)), metavar='N',
                    help='quit normally after N instructions (for testing)')
    args = ap.parse_args()

    imported = fat16.import_folder(args.disk, args.sync_folder) if args.sync_folder else None
    m = Machine(args.rom, disk_path=args.disk, log=print if args.verbose else (lambda s: None))
    try:
        run_window(m, args)
    finally:
        if args.sync_folder:
            m.disk.file.flush()
            fat16.export_folder(args.disk, args.sync_folder, imported)


def run_window(m, args):
    win = sdl.Window('K-PEX', PANEL_H, PANEL_W, scale=args.scale)

    pressed = []          # codes of pressed ADC buttons, the last one wins
    pressed_at = {}       # code -> cycle when the button was pressed
    release_at = {}       # code -> cycle when to release it (if the key was released too early)
    hold = False
    shot = 0
    shown_frame = -1
    last_present = last_title = time.time()
    last_cycles = 0

    def update_adc():
        m.adc.channels[0] = ADC_FOR_CODE[pressed[-1]] if pressed else NO_KEY

    running = True
    while running:
        m.run(CHUNK)
        if args.max_instructions and m.cpu.cycles >= args.max_instructions:
            break
        for code, when in list(release_at.items()):
            if m.cpu.cycles >= when:
                del release_at[code]
                if code in pressed:
                    pressed.remove(code)
                update_adc()
        now = time.time()
        if now - last_present < FRAME_TIME:
            continue
        last_present = now

        for ev in win.events():
            if ev[0] == 'quit':
                running = False
            elif ev[0] in ('down', 'up'):
                key, down = ev[1], ev[0] == 'down'
                if key == sdl.K_ESCAPE:
                    running = False
                elif key in KEYS:
                    code = KEYS[key]
                    if down:
                        print(f'button: code {code} (ADC {ADC_FOR_CODE[code]})')
                        release_at.pop(code, None)
                        if code not in pressed:
                            pressed.append(code)
                        pressed_at[code] = m.cpu.cycles
                        update_adc()
                    elif code in pressed:
                        release_at[code] = max(m.cpu.cycles, pressed_at[code] + MIN_HOLD)
                elif key == ord('p'):
                    if down:
                        print('button: power')
                    m.gpio.set_power_button(down)
                elif down and key not in (ord('h'), ord('s')):
                    print(f'unmapped key: {key:#x}')
                elif key == ord('h') and down:
                    hold = not hold
                    m.gpio.set_hold(hold)
                elif key == ord('s') and down:
                    name = f'screenshot-{shot:03d}.png'
                    m.lcd.save_png(name)
                    print('screenshot saved:', name)
                    shot += 1

        if m.lcd.frame != shown_frame:
            shown_frame = m.lcd.frame
            win.show(m.lcd.landscape_565())

        if now - last_title >= 1:
            mips = (m.cpu.cycles - last_cycles) / (now - last_title) / 1e6
            last_title, last_cycles = now, m.cpu.cycles
            win.set_title(f'K-PEX - {mips:.1f} MIPS, {m.cpu.cycles / 1e6:.0f}M instructions total'
                          f'{"  [HOLD]" if hold else ""}')
    win.close()


if __name__ == '__main__':
    main()
