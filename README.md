# Kingston K-PEX emulator

An emulator of the Kingston K-PEX MP3 player. It runs the original firmware
(`gmp-m6.rom`) and shows the player's screen in a window. There is no sound.

## Requirements

- [PyPy 3](https://pypy.org) (`pypy3`). Plain Python 3 works too, but about 5 times slower.
- The SDL2 library. On Fedora it is the `sdl2-compat` package.
- A firmware file, e.g. `gmp-m6.rom`.

### Firmware

The firmware is not included. Look for it in the Web Archive copy of the old
Kingston site (the support section for K-PEX), or on the Cenix site: Cenix
published all firmware versions from 1.00 to 2.21.

### Windows

A ready-to-run archive for Windows is in
[Releases](https://github.com/3ap/kpexemu/releases/tag/latest):
`kpex-emulator-windows.zip`, rebuilt on every push to master. It already
contains PyPy and SDL2.

1. Unpack the archive.
2. Put the firmware (`KPEX100.rom` or `gmp-m6.rom`) next to `run-kpex.bat`.
3. Run `run-kpex.bat`. On first start it creates a disk image for that firmware.

`mcopy` (mtools) is usually not available on Windows, so copying your own files
to the disk image is not covered yet.

## Running

Prepare a disk image once: the firmware loads its pictures and fonts from it.

```
python3 tools/mkdisk.py gmp-m6.rom disk.img
```

Then start the emulator:

```
pypy3 gui.py gmp-m6.rom --disk disk.img
```

The main menu appears after about 15-20 seconds.

## Your own files

`disk.img` is the player's disk with a FAT16 filesystem. Copy files to it with
`mcopy` from mtools (the partition starts at byte offset 15872). Close the
emulator first.

```
mcopy -i disk.img@@15872 song.mp3 ::/MUSIC/
mcopy -i disk.img@@15872 photo.jpg ::/PHOTO/
mcopy -i disk.img@@15872 book.txt ::/TEXT/
mdir  -i disk.img@@15872 -/ ::            # list the disk contents
```

The player creates MUSIC, PHOTO, TEXT and the other folders itself on first boot.

## Controls

| Key             | Action                                          |
|-----------------|-------------------------------------------------|
| Left / Right    | left / right                                    |
| Up / Down       | up / down                                       |
| Enter           | OK, open the selected item                      |
| Space           | Play, opens the MP3 player directly             |
| Backspace or 7  | Back                                            |
| M or 3          | Menu                                            |
| 1, 2, 4, 5, 6   | other player buttons (purpose unknown yet)      |
| P               | power button (a long press turns the player off) |
| H               | HOLD switch                                     |
| S               | save a screenshot to `screenshot-NNN.png`       |
| Esc             | quit                                            |

## Known issues

- The player turns itself off about 30 seconds after the main menu appears.
- MP3 files show up in the player but do not play: sound is not emulated.
