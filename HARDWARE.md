# Kingston K-PEX: what is known about the hardware

Collected while writing the emulator: from disassembling the firmware
`gmp-m6.rom` (version 2.09, KPEX100/512MB), the Rockbox header for TCC77x
(`docs/rockbox-tcc77x.h`) and forum posts. Guesses are marked as such.

## Components (from a forum)

| Chip | Purpose |
|---|---|
| Telechips TCC767 | CPU, ARM940T core (ARMv4T, ARM + Thumb, MPU instead of MMU) |
| Hynix HY57V281620ET-H | SDRAM, 128 Mbit = 16 MB |
| Samsung K9WAG08U1A | NAND flash |
| Wolfson WM8750 | audio codec |
| Linear LTC3455 | USB power, battery charging |
| Philips TEA5767 | FM receiver |
| NXP PCF8563 | real-time clock |
| - | 220x176 color display (RGB565) |

## Memory map

| Address | Contents |
|---|---|
| `0x00000000` | internal SRAM: exception vectors and stacks (size unknown, ~4 KB used) |
| `0x20000000` | 16 MB SDRAM, mirrored at higher addresses |
| `0x40000000` | ATA storage device (chip select 0) |
| `0x50000000` | display controller (chip select 1) |
| `0x70000000` | the whole firmware image (chip select 3?) |
| `0x80000000` | TCC767 peripheral registers |
| `0x90000000` | TCC767 NAND controller, not used by the firmware |
| `0xF0000000` | SDRAM and external bus controller |

It is unknown what puts the firmware image at `0x70000000`: there is no NOR
flash on the board, probably a boot loader does it.

## Firmware `gmp-m6.rom`

- Header: `Ver:1234` at `0x08`, number of parts (3) at `0x14`, 16-byte entries
  at `0x20`: file offset, first sector, sector count (512-byte sectors), checksum.
- The first 1 MB is code. It starts at `0x70000084`, copies itself to SDRAM
  (`0x20010000`) and the vectors to SRAM, then starts the OS.
- The three parts after the code are resources: RGB565 pictures, fonts, etc.
- Real-time OS similar to uC/OS-II.
- Supported file extensions: `.MP3 .WMA .MP2 .OGG .WAV .ASF .MPX .JPG .TXT
  .M3U .SGS` (`.SGS` are games, GNEX format).
- No games are built into the firmware.

## TCC767 peripherals (`0x80000000 + offset`)

The addresses match TCC77x in Rockbox.

| Offset | Block |
|---|---|
| `0x000` | DAI (I2S to the codec): DADI/DADO samples, DAMR `+0x40` (bit 15 on, 14 transmit, 13 receive) |
| `0x100` | interrupt controller (IEN, CREQ, IREQ, IRQSEL...) |
| `0x200` | timers; timer 0 drives the system tick (interrupt bit 6) |
| `0x300` | GPIO A-E (data, direction, function) |
| `0x400` | clocks and PLL (PLL0CFG bit 20 = PLL locked) |
| `0xA00` | 10-bit ADC (start: channel number to `+0x00`/`+0x80`; result at `+0x84`, bits 26:24 = ready) |
| `0xA10` | "bit length" of a number (`0xB1` -> 8) |
| `0xD00` | USB host, OHCI (interrupt bit 10) |

**Sound output.** Audio goes out through an FIQ: the transmit interrupt I2ST
(bit 5, routed to FIQ by IRQSEL) asks for samples, and the handler at SRAM
`0x40` writes 4 stereo samples to DADO_L/R(0..3) from a ring buffer (zeros if
it is empty), then acknowledges with CREQ = `0x30`. The firmware turns the DAI
transmitter on (DAMR `0xE20` -> `0xCE20`) when entering GAME, and games wait for
their sounds to finish: without these interrupts Battle Armor stops on the
second logo. The emulator raises I2ST at 44.1 kHz / 4 (the real rate is not
decoded) and discards the samples.

**Anti-clone check.** The firmware writes a number to `0x80000A10` and checks
the answer. If it is wrong, it corrupts an OS kernel variable and tasks stop
switching.

## Inputs

| What | Where | Value |
|---|---|---|
| Buttons | ADC channel 0 (resistor ladder) | 1023 = nothing pressed |
| Battery | ADC channel 2 | < 580: "Low Battery!!", power off |
| Charger / USB | ADC channel 4 | > 500: connected (guess) |
| HOLD | GPIOD bit 15 | 0 = locked ("The device is Locked !!") |
| Power / Play | GPIOA bit 15 | 0 = pressed; holding ~1 s turns the player off |

Button code by ADC channel 0 value:

| ADC <= | 40 | 110 | 190 | 270 | 350 | 430 | 500 | 590 | 650 | 700 | 780 | 850 | 980 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| code | 0 | 12 | 13 | 3 | 7 | 4 | 5 | 9 | 8 | 2 | 1 | 11 | 10 |

Known codes: 0 Play (opens the player directly), 13 OK, 5 Menu, 12 Back,
1/2 left/right, 8/9 down/up. The pairs 1/2 and 8/9 swap when the screen is
rotated. The other codes are unknown.

**I2C bus** is bit-banged on GPIOA bits 8 and 9. The clock, FM receiver and
codec are probably on it.

## Storage (`0x40000000`)

- ATA device: registers 4 bytes apart (`+0x04` error, `+0x08` sector count,
  `+0x0C..0x14` LBA, `+0x18` device, `+0x1C` status/command), 16-bit data at `+0x00`.
- Standard commands: IDENTIFY (`0xEC`), READ (`0x20`), WRITE (`0x30`).
- Vendor commands: `0x61`, `0x62`, `0x64` (purpose unknown) and `0x63`, which
  returns 512 bytes of device info; bytes 1-16 look like a serial number.
- Probably a bridge to the NAND rather than a real hard disk.

## Disk layout

- MBR and a single FAT16 partition starting at sector 31, 16 KB clusters,
  label `TELECHIPS`. The partition size comes from the CHS geometry
  (16 heads x 63 sectors).
- The firmware formats a blank disk itself and creates the folders RECORD,
  MUSIC, GAME, PHOTO, TEXT, VIDEO, CONFIG.
- **Firmware resources** are at LBA 4096 + "first sector" from the header
  (LBA 4096-14058), **settings** at sector 1001. Both areas are inside the FAT
  partition but marked free in the FAT. How the real player separates them is
  unknown; the emulator marks these clusters as bad.

## Display (`0x50000000`)

- 8080 bus, 16 bits: `+0` register index, `+2` data.
- Registers like Samsung S6D0110 / Renesas HD667xx: R00 device code, R03 entry
  mode, R21 address, R22 pixel write, R44/R45 window.
- The firmware knows two device codes: `0x8309` (width 220) and `0x8306` (width 224).
- The panel is 176x220 portrait; the firmware writes columns (AM=1), which
  gives a 220x176 landscape picture.
- Frames are written by the CPU pixel by pixel, without DMA.

## Power

- On power off the firmware plays an animation, then loops writing
  GPIOA_DIR = `0xFFFF27FF` and GPIOA, which cuts the power. Which bit holds the
  LTC3455 on is unknown: the same GPIOA_DIR value is also set during normal boot.
- About 30 s after the main menu the player turns itself off; the reason is unknown.

## Not emulated yet

Sound playback (samples are dropped; the WM8750 codec is not emulated), the clock, FM receiver, USB device,
exact timer frequencies.
