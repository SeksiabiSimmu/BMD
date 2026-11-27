#!/usr/bin/env python3
"""Single-file installer container, appended after the Windows executable image.

    [PE image][entry data ...][index][footer (72 bytes)][certificate table, once the file is signed]

footer, little-endian:  magic 8s | pe_size Q | index_offset Q | index_size Q | count I | reserved I | index_sha256 32s
index entry (120 bytes): name 64s | offset Q | size Q | flags I | reserved I | sha256 32s

The native launcher (app/installer/launcher/) reads the same bytes, so the layout must not change
without both sides. Entries are stored uncompressed: nothing has to be unpacked before it can be
verified, and every entry carries its SHA-256 so a damaged installer is detected before anything
from it is executed.

The installer is signed after the container is appended. A signing tool puts the certificate table
at the very end of the file and starts it on an 8-byte boundary, so it may insert up to seven zero
bytes after the footer. The footer is therefore found through the PE header's certificate-table
entry (a file offset) and looked for in the last 79 bytes before that table; nothing but zeros may
follow it. A signed and an unsigned build carry exactly the same container at the same offsets.

Nothing here imports anything outside the standard library.
"""
import hashlib
import os
import re
import struct

MAGIC = b'BMDPKG\x00\x01'
FOOTER = struct.Struct('<8sQQQI I32s')
ENTRY = struct.Struct('<64sQQII32s')
BOOTSTRAP = 1                 # the native launcher extracts it before PowerShell starts
PAD = 7                       # zero bytes a signing tool may insert after the footer (8-byte alignment)
MAX_ENTRIES = 64
MAX_NAME = 64
CHUNK = 1024 * 256
HEADER_PROBE = 64 * 1024      # any real PE header fits well within the first 64 KiB
_NAME = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9._ -]{0,46}[A-Za-z0-9])?$')   # must start and end alphanumeric
_DEVICE = re.compile(r'^(?:con|prn|aux|nul|conin\$|conout\$|com[1-9]|lpt[1-9])(?:\s*\..*)?$', re.I)   # no Windows device names


class ContainerError(Exception):
    """The file is not an intact installer (truncated, edited, or not an installer at all)."""


def _sha(data):
    return hashlib.sha256(data).digest()


def build(pe_image, entries):
    """Return the full installer file: `pe_image` followed by the container.

    `entries` is a list of (name, bytes, bootstrap) in storage order. The result depends only on
    the inputs, so two builds of the same sources produce the same file. `pe_image` must be the
    unsigned launcher: signing comes last, so that the signature covers the container.
    """
    if _image_end(pe_image[:HEADER_PROBE], len(pe_image)) != len(pe_image):
        raise ValueError('the launcher image is already signed; sign the finished installer instead')
    payload = bytearray(pe_image)
    index = bytearray()
    count = len(entries)
    if not 0 < count <= MAX_ENTRIES:
        raise ValueError(f'a container holds 1..{MAX_ENTRIES} entries, not {count}')
    names = set()
    for name, data, bootstrap in entries:
        encoded = name.encode('utf-8')
        if _DEVICE.match(name) or not _NAME.match(name) or len(encoded) >= MAX_NAME:
            raise ValueError(f'unsafe or too long entry name: {name!r}')
        if name.lower() in names:
            raise ValueError(f'duplicate entry name: {name}')
        names.add(name.lower())
        while len(payload) % 16:                      # entries start on a 16-byte boundary
            payload.append(0)
        index += ENTRY.pack(encoded.ljust(MAX_NAME, b'\0'), len(payload), len(data),
                            BOOTSTRAP if bootstrap else 0, 0, _sha(data))
        payload += data
    index_offset = len(payload)
    payload += index
    payload += FOOTER.pack(MAGIC, len(pe_image), index_offset, len(index), count, 0, _sha(bytes(index)))
    return bytes(payload)


def _image_end(header, file_size):
    """File offset where the installer's data ends (start of the certificate table, or the end).

    `header` is the beginning of the file. Raises ContainerError when it is not a Windows
    executable, or when the certificate table it announces lies outside the file.
    """
    def need(condition):
        if not condition:
            raise ContainerError('This file is not a Brave Music Downloader installer '
                                 '(it has no Windows executable header).')

    need(len(header) >= 0x40 and header[:2] == b'MZ')
    pe = struct.unpack_from('<I', header, 0x3c)[0]
    need(pe + 24 + 2 <= len(header) and header[pe:pe + 4] == b'PE\0\0')
    optional = pe + 24
    directories = {0x10b: 96, 0x20b: 112}.get(struct.unpack_from('<H', header, optional)[0])   # PE32, PE32+
    need(directories is not None)
    need(optional + directories + 4 <= len(header))
    if struct.unpack_from('<I', header, optional + directories - 4)[0] < 5:    # NumberOfRvaAndSizes
        return file_size                           # no room for a certificate entry: not signed
    need(optional + directories + 5 * 8 <= len(header))
    table_offset, table_size = struct.unpack_from('<II', header, optional + directories + 4 * 8)
    if table_size == 0:
        return file_size
    if table_offset > file_size or table_size > file_size - table_offset:
        raise ContainerError('The installer file is damaged (its certificate table lies outside the file).')
    return table_offset


def _footer(tail):
    """Locate the footer in `tail`: the last FOOTER.size + PAD bytes before the certificate table.

    Returns (padding, footer). The footer ends `padding` bytes before the table, 0 to PAD; the
    bytes in between must be zeros, which is all a signing tool inserts for its 8-byte alignment.
    """
    for pad in range(PAD + 1):
        end = len(tail) - pad
        if end < FOOTER.size:
            break
        if tail[end - FOOTER.size:end - FOOTER.size + 8] != MAGIC:
            continue
        if tail[end:].strip(b'\0'):
            raise ContainerError('The installer file is damaged (it has extra data after its content index).')
        return pad, tail[end - FOOTER.size:end]
    raise ContainerError('This file is not a Brave Music Downloader installer.')


def _load(file_size, table, footer_end, footer, read_index):
    """Validate the footer and the index; `read_index(offset, size)` returns the index bytes.

    `table` is where the certificate table starts (the file size when the file is not signed) and
    `footer_end` where the footer ends; the index ends where the footer begins.
    """
    magic, pe_size, index_offset, index_size, count, reserved, index_sha = FOOTER.unpack(footer)
    if magic != MAGIC:
        raise ContainerError('This file is not a Brave Music Downloader installer.')
    if reserved != 0 or not 0 < count <= MAX_ENTRIES or not 0 < pe_size <= index_offset:
        raise ContainerError('The installer file is damaged.')
    index_end = footer_end - FOOTER.size
    if index_offset > index_end or index_size != count * ENTRY.size or index_end - index_offset != index_size:
        raise ContainerError('The installer file is truncated or was modified after it was built.')
    index = read_index(index_offset, index_size)
    if len(index) != index_size:
        raise ContainerError('The installer file is truncated.')
    if _sha(index) != index_sha:
        raise ContainerError('The installer file is damaged (its content index does not match).')
    entries, names = [], set()
    for i in range(count):
        raw_name, offset, size, flags, entry_reserved, digest = ENTRY.unpack_from(index, i * ENTRY.size)
        name = raw_name.split(b'\0', 1)[0].decode('utf-8', 'replace')
        damaged = (not _NAME.match(name) or _DEVICE.match(name) or raw_name[len(raw_name.split(b'\0', 1)[0]):].strip(b'\0')
                   or offset < pe_size or offset + size > index_offset
                   or flags & ~BOOTSTRAP or entry_reserved != 0 or name.lower() in names)
        if damaged:
            raise ContainerError('The installer file is damaged (an entry is out of bounds).')
        names.add(name.lower())
        entries.append({'name': name, 'offset': offset, 'size': size, 'flags': flags,
                        'sha256': digest.hex(), 'bootstrap': bool(flags & BOOTSTRAP)})
    return {'pe_size': pe_size, 'index_offset': index_offset, 'entry_count': count, 'entries': entries,
            'data_end': footer_end, 'signed': table != file_size}


def parse(data):
    """Parse the container of the installer held in memory (bytes). Returns a dict; raises ContainerError."""
    data = bytes(data)
    if len(data) < FOOTER.size:
        raise ContainerError('The file is too small to be an installer.')
    end = _image_end(data[:HEADER_PROBE], len(data))
    if end < FOOTER.size:
        raise ContainerError('The file is too small to be an installer.')
    pad, footer = _footer(data[max(0, end - FOOTER.size - PAD):end])
    return _load(len(data), end, end - pad, footer, lambda offset, size: data[offset:offset + size])


def read(path):
    """Parse the container of the installer file `path` (reads only the header, the index and the footer)."""
    size = os.path.getsize(path)
    with open(path, 'rb') as handle:
        if size < FOOTER.size:
            raise ContainerError('The file is too small to be an installer.')
        end = _image_end(handle.read(HEADER_PROBE), size)
        if end < FOOTER.size:
            raise ContainerError('The file is too small to be an installer.')
        handle.seek(max(0, end - FOOTER.size - PAD))
        tail = handle.read(FOOTER.size + PAD)
        if len(tail) < FOOTER.size:
            raise ContainerError('The installer file is truncated.')
        pad, footer = _footer(tail)

        def read_index(offset, length):
            handle.seek(offset)
            return handle.read(length)

        return _load(size, end, end - pad, footer, read_index)


def by_name(container):
    return {entry['name']: entry for entry in container['entries']}


def entry_data(path, entry):
    """The complete content of one entry (for small files only)."""
    with open(path, 'rb') as handle:
        handle.seek(entry['offset'])
        return handle.read(entry['size'])


def copy_entry(path, entry, destination, expected=None, report=None):
    """Stream one entry into `destination` and verify its SHA-256 (and `expected`, when given).

    The data is written to `<destination>.partial` and only renamed into place after the hash
    matches. `report(done, total)` is called with byte counts while copying.
    """
    from pathlib import Path
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + '.partial')
    digest = hashlib.sha256()
    done = 0
    with open(path, 'rb') as source, open(partial, 'wb') as out:
        source.seek(entry['offset'])
        remaining = entry['size']
        while remaining:
            chunk = source.read(min(CHUNK, remaining))
            if not chunk:
                raise ContainerError('The installer file is truncated.')
            out.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            remaining -= len(chunk)
            if report:
                report(done, entry['size'])
        out.flush()
        os.fsync(out.fileno())
    actual = digest.hexdigest()
    if actual != entry['sha256'] or (expected and actual != expected.lower()):
        partial.unlink(missing_ok=True)
        if actual != entry['sha256']:
            raise ContainerError(f"{entry['name']}: content does not match its SHA-256 "
                                 f'(expected {entry["sha256"][:12]}…, got {actual[:12]}…). The installer file is damaged.')
        raise ContainerError(f"{entry['name']}: the bundled file does not match the version pinned for this release "
                             f'(expected {expected[:12]}…, got {actual[:12]}…). The installer file is damaged or was '
                             'assembled from the wrong files.')
    os.replace(partial, destination)
    return actual
