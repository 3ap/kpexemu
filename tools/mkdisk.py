#!/usr/bin/env python3
"""Prepare a player disk image from a firmware file.

    python3 tools/mkdisk.py gmp-m6.rom disk.img

What it does:
1. If the disk has no filesystem, creates the same one the firmware itself
   creates: MBR, a single FAT16 partition from sector 31, 16 KB clusters,
   label TELECHIPS.
2. Writes the firmware resources (pictures, fonts) where the firmware looks for
   them: LBA = 4096 + "first sector" from the firmware file header.
3. Marks as "bad" in the FAT the clusters the firmware uses bypassing the
   filesystem (resources and the settings sector 1001). Otherwise files copied
   to the disk could land on top of them.

If a filesystem already exists, the files on it are left alone.
Files are copied to the disk with mcopy from mtools, see README.md.
"""

import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from kpex.machine import parse_rom_header  # noqa: E402

SECTOR = 512
SYSTEM_AREA_LBA = 4096
SETTINGS_LBA = 1001          # the firmware stores its settings here directly

# Filesystem parameters, the same as the firmware uses when formatting
PART_LBA = 31
SECTORS_PER_CLUSTER = 32
RESERVED = 1
FATS = 2
ROOT_ENTRIES = 512
SECTORS_PER_FAT = 128
FAT_LBA = PART_LBA + RESERVED
ROOT_LBA = FAT_LBA + FATS * SECTORS_PER_FAT
DATA_LBA = ROOT_LBA + ROOT_ENTRIES * 32 // SECTOR
BAD_CLUSTER = 0xFFF7


def cluster_of(lba):
    return (lba - DATA_LBA) // SECTORS_PER_CLUSTER + 2


def has_filesystem(f):
    f.seek(0)
    mbr = f.read(SECTOR)
    return mbr[510:512] == b'\x55\xAA' and mbr[446 + 4] in (0x04, 0x06, 0x0E)


def create_filesystem(f, total_sectors):
    # The firmware takes the disk size from the CHS geometry (16 heads, 63 sectors
    # per track) as reported by IDENTIFY, not from the full sector count.
    chs_sectors = min(total_sectors // (16 * 63), 16383) * 16 * 63
    part_sectors = chs_sectors - PART_LBA
    # MBR: a single active FAT16 partition (type 0x06)
    mbr = bytearray(SECTOR)
    mbr[446:462] = struct.pack('<B3sB3sII', 0x80, b'\x01\x10\x00', 0x06, b'\x0F\x3F\x0F',
                               PART_LBA, part_sectors)
    mbr[510:512] = b'\x55\xAA'
    # Partition boot sector
    bs = bytearray(SECTOR)
    bs[0:3] = b'\xEB\x5A\x90'
    bs[3:11] = b'MSDOS5.0'
    struct.pack_into('<HBHBHHBHHHII', bs, 11, SECTOR, SECTORS_PER_CLUSTER, RESERVED, FATS,
                     ROOT_ENTRIES, 0, 0xF8, SECTORS_PER_FAT, 63, 16, PART_LBA, part_sectors)
    struct.pack_into('<BBBI11s8s', bs, 36, 0, 0, 0x29, 0, b'TELECHIPS  ', b'FAT16   ')
    bs[510:512] = b'\x55\xAA'
    fat = bytearray(SECTORS_PER_FAT * SECTOR)
    fat[0:4] = b'\xF8\xFF\xFF\xFF'
    root = bytearray(ROOT_ENTRIES * 32)
    root[0:11] = b'TELECHIPS  '
    root[11] = 0x08          # volume label
    for lba, data in ((0, mbr), (PART_LBA, bs), (ROOT_LBA, root)):
        f.seek(lba * SECTOR)
        f.write(data)
    for n in range(FATS):
        f.seek((FAT_LBA + n * SECTORS_PER_FAT) * SECTOR)
        f.write(fat)


def protect_clusters(f, clusters):
    """Mark clusters as bad in every FAT copy. Returns the clusters that were
    already used by files (those are left alone)."""
    busy = []
    for n in range(FATS):
        base = (FAT_LBA + n * SECTORS_PER_FAT) * SECTOR
        for c in clusters:
            f.seek(base + 2 * c)
            val = struct.unpack('<H', f.read(2))[0]
            if val in (0, BAD_CLUSTER):
                f.seek(base + 2 * c)
                f.write(struct.pack('<H', BAD_CLUSTER))
            elif n == 0:
                busy.append(c)
    return busy


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('rom')
    ap.add_argument('disk')
    ap.add_argument('--size-mb', type=int, default=512, help='size of a new image')
    args = ap.parse_args()

    rom = open(args.rom, 'rb').read()
    info = parse_rom_header(rom)
    if not os.path.exists(args.disk):
        with open(args.disk, 'wb') as f:
            f.truncate(args.size_mb << 20)
    with open(args.disk, 'r+b') as f:
        f.seek(0, os.SEEK_END)
        total = f.tell() // SECTOR
        if has_filesystem(f):
            print('filesystem already exists, leaving files alone')
        else:
            create_filesystem(f, total)
            print(f'created a FAT16 filesystem ({(total - PART_LBA) * SECTOR >> 20} MB)')

        protected = {cluster_of(SETTINGS_LBA)}
        for n, p in enumerate(info['parts']):
            lba = SYSTEM_AREA_LBA + p['first_sector']
            f.seek(lba * SECTOR)
            f.write(rom[p['offset']:p['offset'] + p['size']])
            protected.update(range(cluster_of(lba), cluster_of(lba + p['sectors'] - 1) + 1))
            print(f'resources, part {n}: LBA {lba}..{lba + p["sectors"] - 1}')

        busy = protect_clusters(f, sorted(protected))
        print(f'protected clusters: {len(protected) - len(busy)}')
        if busy:
            print(f'WARNING: clusters {busy} are already used by files; the resources '
                  f'written over them may have corrupted those files')


if __name__ == '__main__':
    main()
