"""Read and rebuild the FAT16 filesystem on a player disk image, and keep it in
sync with a plain folder on the host.

The disk image (see tools/mkdisk.py) has an MBR and one FAT16 partition. Some
clusters are marked bad on purpose: the firmware keeps its resources and
settings there, bypassing the filesystem. Everything here leaves them alone.

Syncing works like this:
* import_folder(image, folder) runs before the emulator starts. It rebuilds the
  filesystem in the image so that it contains exactly the files from `folder`.
* export_folder(image, folder, imported) runs after the emulator stops. It copies
  files the player created or changed back to `folder`, and deletes from `folder`
  the files that were imported but no longer exist on the player.
So between runs `folder` is the real storage and the image is a working copy.
A file in `folder` is only overwritten or deleted if nobody changed it on the
host since the import; host changes made during a run always win.
"""

import os
import struct
import time

SECTOR = 512
ATTR_READONLY, ATTR_HIDDEN, ATTR_SYSTEM = 0x01, 0x02, 0x04
ATTR_VOLUME, ATTR_DIR, ATTR_ARCHIVE = 0x08, 0x10, 0x20
ATTR_LFN = 0x0F
BAD_CLUSTER = 0xFFF7
END_OF_CHAIN = 0xFFFF

# Folders the firmware uses; they are created in the host folder so users see them
STANDARD_FOLDERS = ('MUSIC', 'PHOTO', 'TEXT', 'GAME', 'VIDEO', 'RECORD')

SHORT_NAME_CHARS = set('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!#$%&\'()-@^_`{}~')
WINDOWS_BAD_CHARS = set('<>:"/\\|?*')


class FatError(Exception):
    pass


class Entry:
    def __init__(self, name, attr, cluster, size, mtime):
        self.name = name
        self.attr = attr
        self.cluster = cluster
        self.size = size
        self.mtime = mtime

    @property
    def is_dir(self):
        return bool(self.attr & ATTR_DIR)


# --- small helpers ----------------------------------------------------------------

def dos_datetime(timestamp):
    t = time.localtime(timestamp)
    year = min(max(t.tm_year, 1980), 2107)
    date = ((year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday
    clock = (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2)
    return date, clock


def from_dos_datetime(date, clock):
    if date == 0:
        return None
    try:
        return time.mktime((1980 + (date >> 9), (date >> 5) & 0xF, date & 0x1F,
                            clock >> 11, (clock >> 5) & 0x3F, (clock & 0x1F) * 2, 0, 0, -1))
    except (OverflowError, ValueError):
        return None


def lfn_checksum(short):
    s = 0
    for b in short:
        s = (((s & 1) << 7) + (s >> 1) + b) & 0xFF
    return s


def short_name(name, used):
    """Make an 8.3 name the way Windows does (e.g. "TresureHunter_k.sgs" ->
    "TRESUR~1SGS"). Returns (11 bytes, whether a long name entry is needed)."""
    base, dot, ext = name.rpartition('.')
    if not dot or not base:
        base, ext = name, ''

    def clean(s):
        out, lossy = [], False
        for ch in s.upper():
            if ch in SHORT_NAME_CHARS:
                out.append(ch)
            elif ch in ' .':
                lossy = True
            else:
                out.append('_')
                lossy = True
        return ''.join(out), lossy

    b, lossy_b = clean(base.lstrip('.'))
    e, lossy_e = clean(ext)
    exact = not lossy_b and not lossy_e and 0 < len(b) <= 8 and len(e) <= 3
    if exact:
        candidate = (b.ljust(8) + e.ljust(3)).encode('ascii')
        if candidate not in used:
            used.add(candidate)
            return candidate, name != (b + ('.' + e if e else ''))
    b = b or '_'
    for n in range(1, 1000000):
        tail = f'~{n}'
        candidate = (b[:8 - len(tail)] + tail).ljust(8) + e[:3].ljust(3)
        candidate = candidate.encode('ascii')
        if candidate not in used:
            used.add(candidate)
            return candidate, True
    raise FatError(f'too many similar names: {name}')


def lfn_entries(name, short):
    """Long file name directory entries, in the order they are stored on disk."""
    chars = [ord(c) for c in name]
    if any(c > 0xFFFF for c in chars):
        chars = [c if c <= 0xFFFF else ord('_') for c in chars]
    if len(chars) > 255:
        raise FatError(f'name too long: {name}')
    if len(chars) % 13:
        chars += [0] + [0xFFFF] * (12 - len(chars) % 13)
    checksum = lfn_checksum(short)
    count = len(chars) // 13
    entries = []
    for n in range(count):
        part = chars[n * 13:(n + 1) * 13]
        e = bytearray(32)
        e[0] = (n + 1) | (0x40 if n == count - 1 else 0)
        struct.pack_into('<5H', e, 1, *part[0:5])
        e[11] = ATTR_LFN
        e[13] = checksum
        struct.pack_into('<6H', e, 14, *part[5:11])
        struct.pack_into('<2H', e, 28, *part[11:13])
        entries.append(bytes(e))
    return list(reversed(entries))


def dir_entry(short, attr, cluster, size, mtime):
    e = bytearray(32)
    e[0:11] = short
    e[11] = attr
    date, clock = dos_datetime(mtime)
    struct.pack_into('<HH', e, 14, clock, date)    # creation time / date
    struct.pack_into('<H', e, 18, date)            # last access date
    struct.pack_into('<HH', e, 22, clock, date)    # write time / date
    struct.pack_into('<HI', e, 26, cluster, size)
    return bytes(e)


def safe_host_name(name):
    name = ''.join('_' if ch in WINDOWS_BAD_CHARS or ord(ch) < 32 else ch for ch in name)
    return name.rstrip(' .') or '_'


# --- the filesystem -----------------------------------------------------------------

class Fat16:
    def __init__(self, path):
        self.f = open(path, 'r+b')
        mbr = self.read(0, 1)
        if mbr[510:512] != b'\x55\xAA':
            raise FatError(f'{path}: no partition table')
        self.part_lba = struct.unpack_from('<I', mbr, 446 + 8)[0]
        bs = self.read(self.part_lba, 1)
        (bps, self.spc, reserved, self.nfats, self.root_entries, total16, media,
         self.spf) = struct.unpack_from('<HBHBHHBH', bs, 11)
        if bps != SECTOR or bs[54:59] != b'FAT16' or not self.spc:
            raise FatError(f'{path}: not a FAT16 partition')
        total = total16 or struct.unpack_from('<I', bs, 32)[0]
        self.media = media
        self.fat_lba = self.part_lba + reserved
        self.root_lba = self.fat_lba + self.nfats * self.spf
        self.data_lba = self.root_lba + self.root_entries * 32 // SECTOR
        self.cluster_bytes = self.spc * SECTOR
        data_clusters = (total - (self.data_lba - self.part_lba)) // self.spc
        self.fat = list(struct.unpack(f'<{self.spf * SECTOR // 2}H', self.read(self.fat_lba, self.spf)))
        self.last_cluster = min(data_clusters + 1, len(self.fat) - 1)

    def close(self):
        self.f.close()

    def read(self, lba, count):
        self.f.seek(lba * SECTOR)
        data = self.f.read(count * SECTOR)
        return data + bytes(count * SECTOR - len(data))

    def write(self, lba, data):
        self.f.seek(lba * SECTOR)
        self.f.write(data)

    def cluster_lba(self, c):
        return self.data_lba + (c - 2) * self.spc

    # --- reading ---------------------------------------------------------------------

    def chain(self, first):
        seen = set()
        c = first
        while 2 <= c <= self.last_cluster and c not in seen:
            seen.add(c)
            yield c
            c = self.fat[c]

    def read_chain(self, first, size=None):
        out = bytearray()
        for c in self.chain(first):
            out += self.read(self.cluster_lba(c), self.spc)
            if size is not None and len(out) >= size:
                break
        return bytes(out if size is None else out[:size])

    def list_dir(self, cluster):
        raw = self.read(self.root_lba, self.root_entries * 32 // SECTOR) if cluster == 0 \
            else self.read_chain(cluster)
        entries, lfn = [], {}
        for off in range(0, len(raw), 32):
            e = raw[off:off + 32]
            if e[0] == 0x00:
                break
            if e[0] == 0xE5:
                lfn = {}
                continue
            attr = e[11]
            if attr == ATTR_LFN:
                seq = e[0] & 0x1F
                part = struct.unpack_from('<5H', e, 1) + struct.unpack_from('<6H', e, 14) + \
                    struct.unpack_from('<2H', e, 28)
                lfn[seq] = (e[13], part)
                continue
            if attr & ATTR_VOLUME:
                lfn = {}
                continue
            short = e[0:11]
            name = None
            if lfn and all(cs == lfn_checksum(short) for cs, _ in lfn.values()):
                chars = []
                for seq in sorted(lfn):
                    chars.extend(lfn[seq][1])
                text = []
                for ch in chars:
                    if ch in (0, 0xFFFF):
                        break
                    text.append(chr(ch))
                name = ''.join(text) or None
            lfn = {}
            if name is None:
                base = short[0:8].decode('cp437').rstrip()
                ext = short[8:11].decode('cp437').rstrip()
                if short[0] == 0x05:
                    base = 'å' + base[1:]
                # Windows NT lower-case flags
                if e[12] & 0x08:
                    base = base.lower()
                if e[12] & 0x10:
                    ext = ext.lower()
                name = base + ('.' + ext if ext else '')
            if name in ('.', '..'):
                continue
            write_time, write_date = struct.unpack_from('<HH', e, 22)
            first, size = struct.unpack_from('<HI', e, 26)
            entries.append(Entry(name, attr, first, size, from_dos_datetime(write_date, write_time)))
        return entries

    def walk(self, cluster=0, prefix='', depth=0):
        """Yield (relative path, Entry) for every file and folder."""
        if depth > 32:
            return
        for entry in self.list_dir(cluster):
            path = f'{prefix}/{entry.name}' if prefix else entry.name
            yield path, entry
            if entry.is_dir and entry.cluster:
                yield from self.walk(entry.cluster, path, depth + 1)

    # --- rebuilding ------------------------------------------------------------------

    def rebuild(self, folder):
        """Replace everything on the filesystem with the contents of `folder`.
        Returns {relative path: host mtime} for files and {path: None} for folders."""
        tree = scan_folder(folder)
        cluster_bytes = self.cluster_bytes

        # Plan first, so that nothing is overwritten if the files do not fit.
        def entries_needed(items):
            n = 2   # "." and ".."
            for name, _ in items:
                n += 1 + (len(name) + 12) // 13
            return n

        def clusters_needed(node, is_root=False):
            total = 0
            if not is_root:
                total += -(-entries_needed(node) * 32 // cluster_bytes)
            for _, item in node:
                if isinstance(item, list):
                    total += clusters_needed(item)
                else:
                    total += -(-item[1] // cluster_bytes)
            return total

        root_entries = 1 + sum(1 + (len(name) + 12) // 13 for name, _ in tree)
        if root_entries > self.root_entries:
            raise FatError(f'too many items in the top folder ({len(tree)})')
        free = [c for c in range(2, self.last_cluster + 1) if self.fat[c] != BAD_CLUSTER]
        need = clusters_needed(tree, is_root=True)
        if need > len(free):
            raise FatError(f'the files do not fit on the player disk: need '
                           f'{need * cluster_bytes >> 20} MB, have {len(free) * cluster_bytes >> 20} MB')

        fat = [BAD_CLUSTER if v == BAD_CLUSTER else 0 for v in self.fat]
        fat[0] = 0xFF00 | self.media
        fat[1] = 0xFFFF
        free_iter = iter(free)
        written = {}

        def allocate(count):
            clusters = [next(free_iter) for _ in range(count)]
            for a, b in zip(clusters, clusters[1:]):
                fat[a] = b
            if clusters:
                fat[clusters[-1]] = END_OF_CHAIN
            return clusters

        def write_clusters(clusters, data):
            for n, c in enumerate(clusters):
                chunk = data[n * cluster_bytes:(n + 1) * cluster_bytes]
                self.write(self.cluster_lba(c), chunk.ljust(cluster_bytes, b'\0'))

        def build_dir(node, my_cluster, parent_cluster, prefix):
            """Write files and subfolders of `node`; return directory entry bytes."""
            used = set()
            out = []
            if my_cluster:
                now = time.time()
                out.append(dir_entry(b'.          ', ATTR_DIR, my_cluster, 0, now))
                out.append(dir_entry(b'..         ', ATTR_DIR, parent_cluster, 0, now))
            for name, item in node:
                short, needs_lfn = short_name(name, used)
                path = f'{prefix}/{name}' if prefix else name
                if isinstance(item, list):
                    count = -(-entries_needed(item) * 32 // cluster_bytes)
                    clusters = allocate(count)
                    mtime = os.path.getmtime(os.path.join(folder, *path.split('/')))
                    written[path] = None
                    data = build_dir(item, clusters[0], my_cluster, path)
                    write_clusters(clusters, data)
                    entry = dir_entry(short, ATTR_DIR, clusters[0], 0, mtime)
                else:
                    host_path, size, mtime = item
                    written[path] = mtime
                    clusters = allocate(-(-size // cluster_bytes))
                    with open(host_path, 'rb') as src:
                        for n, c in enumerate(clusters):
                            chunk = src.read(cluster_bytes)
                            self.write(self.cluster_lba(c), chunk.ljust(cluster_bytes, b'\0'))
                    entry = dir_entry(short, ATTR_ARCHIVE, clusters[0] if clusters else 0, size, mtime)
                if needs_lfn:
                    out.extend(lfn_entries(name, short))
                out.append(entry)
            return b''.join(out)

        root = bytearray(self.root_entries * 32)
        label = dir_entry(self.volume_label(), ATTR_VOLUME, 0, 0, time.time())
        data = label + build_dir(tree, 0, 0, '')
        root[:len(data)] = data
        self.write(self.root_lba, bytes(root))
        packed = struct.pack(f'<{len(fat)}H', *fat)
        for n in range(self.nfats):
            self.write(self.fat_lba + n * self.spf, packed)
        self.fat = fat
        self.f.flush()
        return written

    def volume_label(self):
        bs = self.read(self.part_lba, 1)
        label = bs[43:54]
        return label if label.strip() else b'NO NAME    '


def scan_folder(folder):
    """Folder contents as a sorted list of (name, item): item is a list for a
    subfolder and (path, size, mtime) for a file."""
    out = []
    try:
        names = sorted(os.listdir(folder), key=str.lower)
    except FileNotFoundError:
        return out
    seen = set()
    for name in names:
        path = os.path.join(folder, name)
        if name.lower() in seen or name.startswith('.') or os.path.islink(path):
            continue
        seen.add(name.lower())
        if os.path.isdir(path):
            out.append((name, scan_folder(path)))
        elif os.path.isfile(path):
            st = os.stat(path)
            out.append((name, (path, st.st_size, st.st_mtime)))
    return out


# --- syncing with a host folder ---------------------------------------------------

def import_folder(image, folder, log=print):
    """Before a run: make the player disk contain exactly the files in `folder`.
    On first use (no folder yet) the folder is filled from the disk first."""
    fs = Fat16(image)
    try:
        if not os.path.isdir(folder):
            os.makedirs(folder)
            export_files(fs, folder, {}, log)
        for name in STANDARD_FOLDERS:
            existing = {n.lower() for n in os.listdir(folder)}
            if name.lower() not in existing:
                os.makedirs(os.path.join(folder, name))
        written = fs.rebuild(folder)
        files = sum(1 for mtime in written.values() if mtime is not None)
        log(f'[sync] {folder} -> player disk: {files} files')
        return written
    finally:
        fs.close()


def export_folder(image, folder, imported, log=print):
    """After a run: copy new and changed files from the player disk to `folder`,
    and delete from `folder` what the player deleted."""
    fs = Fat16(image)
    try:
        export_files(fs, folder, imported, log)
    finally:
        fs.close()


def export_files(fs, folder, imported, log):
    """`imported` is what import_folder returned: {path: host mtime or None}."""
    imported_lower = {p.lower(): m for p, m in imported.items()}

    def unchanged_on_host(path, target):
        mtime = imported_lower.get(path.lower())
        return mtime is not None and os.path.isfile(target) and os.path.getmtime(target) == mtime

    on_disk = {}
    for path, entry in fs.walk():
        host = '/'.join(safe_host_name(part) for part in path.split('/'))
        on_disk[host] = entry
    copied = kept = 0
    for path, entry in sorted(on_disk.items()):
        target = os.path.join(folder, *path.split('/'))
        if entry.is_dir:
            if not os.path.exists(target) and path.lower() not in imported_lower:
                os.makedirs(target)
            continue
        if os.path.exists(target) or path.lower() in imported_lower:
            # The file was on the host already: replace it only if the host copy
            # is the one we imported (the user did not touch it during the run).
            if not unchanged_on_host(path, target):
                if os.path.exists(target):
                    kept += 1
                continue
        data = fs.read_chain(entry.cluster, entry.size)[:entry.size] if entry.cluster else b''
        if os.path.isfile(target):
            with open(target, 'rb') as f:
                if f.read() == data:
                    continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + '.part'
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, target)
        if entry.mtime:
            os.utime(target, (entry.mtime, entry.mtime))
        copied += 1
    # Delete what the player deleted, but only files that are still exactly as imported.
    on_disk_lower = {p.lower() for p in on_disk}
    removed = 0
    for path in sorted(imported, key=len, reverse=True):
        if path.lower() in on_disk_lower:
            continue
        target = os.path.join(folder, *path.split('/'))
        if imported[path] is None:
            if os.path.isdir(target) and not os.listdir(target):
                os.rmdir(target)
        elif unchanged_on_host(path, target):
            os.remove(target)
            removed += 1
    msg = f'[sync] player disk -> {folder}: {copied} files copied, {removed} deleted'
    if kept:
        msg += f', {kept} changed on the host during the run were kept'
    log(msg)
