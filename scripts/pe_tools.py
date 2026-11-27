#!/usr/bin/env python3
"""Read what the release build needs to know about a Windows program (standard library only).

    pe_info(data)                    header facts, imports, resource types, manifest and version
    program_problems(info, version)  why the program is not the setup program, whatever follows it in the file
    launcher_problems(data, version) why `data` is not a usable setup launcher image (empty: it is)

scripts/build_launcher.py and scripts/build_release.py share this parser, so both check the same
facts. It reads only the headers and the import, resource and version structures of the file.
"""
import struct

X64 = 0x8664
GUI, CONSOLE = 2, 3
IMPORT, RESOURCE, CERTIFICATE = 1, 2, 4                    # data directory indexes
RT_ICON, RT_GROUP_ICON, RT_VERSION, RT_MANIFEST = 3, 14, 16, 24
LAUNCHER_IMPORTS = {'kernel32.dll', 'user32.dll', 'shell32.dll'}   # all the launcher needs


def _u16(data, at):
    return struct.unpack_from('<H', data, at)[0]


def _u32(data, at):
    return struct.unpack_from('<I', data, at)[0]


def parse_node(blob, at, end):
    """One node of a version resource: (key, value bytes, [child nodes])."""
    length, value_length, kind = struct.unpack_from('<HHH', blob, at)
    if length < 8 or at + length > end:
        raise ValueError('damaged version resource')
    key_end = at + 6
    while key_end + 2 <= at + length and blob[key_end:key_end + 2] != b'\0\0':
        key_end += 2
    key = blob[at + 6:key_end].decode('utf-16-le', 'replace')
    value_at = (key_end + 2 + 3) & ~3                           # values start on a 32-bit boundary
    value_size = value_length * (2 if kind else 1)
    value = blob[value_at:value_at + value_size]
    children, child = [], (value_at + value_size + 3) & ~3
    while child + 8 <= at + length and _u16(blob, child):
        children.append(parse_node(blob, child, at + length))
        child = (child + _u16(blob, child) + 3) & ~3
    return key, value, children


def parse_version(blob):
    """{'file': (a, b, c, d), 'product': (a, b, c, d), 'strings': {name: value}, 'translation': (lang, codepage)}"""
    _, value, children = parse_node(blob, 0, len(blob))
    result = {'file': None, 'product': None, 'strings': {}, 'translation': None}
    if len(value) >= 24 and _u32(value, 0) == 0xFEEF04BD:
        file_ms, file_ls, product_ms, product_ls = struct.unpack_from('<IIII', value, 8)
        result['file'] = (file_ms >> 16, file_ms & 0xFFFF, file_ls >> 16, file_ls & 0xFFFF)
        result['product'] = (product_ms >> 16, product_ms & 0xFFFF, product_ls >> 16, product_ls & 0xFFFF)
    for node in children:
        if node[0] == 'StringFileInfo':
            for table in node[2]:
                for string in table[2]:
                    result['strings'][string[0]] = string[1].decode('utf-16-le', 'replace').rstrip('\0')
        elif node[0] == 'VarFileInfo':
            for translation in node[2]:
                if translation[0] == 'Translation' and len(translation[1]) >= 4:
                    result['translation'] = struct.unpack_from('<HH', translation[1], 0)
    return result


def pe_info(data):
    """Facts about the Windows program in `data` (bytes). Raises ValueError when it is not one."""
    data = bytes(data)
    if len(data) < 0x40 or data[:2] != b'MZ':
        raise ValueError('not a Windows program (no MZ header)')
    pe = _u32(data, 0x3c)
    if pe + 24 > len(data) or data[pe:pe + 4] != b'PE\0\0':
        raise ValueError('not a Windows program (no PE signature)')
    machine, section_count = struct.unpack_from('<HH', data, pe + 4)
    optional_size, characteristics = struct.unpack_from('<HH', data, pe + 20)
    optional = pe + 24
    if optional_size < 70 or optional + optional_size > len(data):
        raise ValueError('truncated optional header')
    magic = _u16(data, optional)
    if magic not in (0x10b, 0x20b):
        raise ValueError('unknown optional header format')
    plus = magic == 0x20b
    directory_count = min(_u32(data, optional + (108 if plus else 92)), 16)
    directories = optional + (112 if plus else 96)
    directory_count = min(directory_count, max(0, optional + optional_size - directories) // 8)
    entries = [struct.unpack_from('<II', data, directories + 8 * i) for i in range(directory_count)]
    sections = []
    for i in range(section_count):
        at = optional + optional_size + 40 * i
        if at + 40 > len(data):
            raise ValueError('truncated section table')
        virtual_size, rva, raw_size, raw = struct.unpack_from('<IIII', data, at + 8)
        sections.append((data[at:at + 8].rstrip(b'\0').decode('ascii', 'replace'), rva, raw, raw_size))
    header_size = _u32(data, optional + 60)
    image_end = max([header_size] + [raw + raw_size for _, _, raw, raw_size in sections if raw_size])

    def offset(rva, size):
        for _, base, raw, raw_size in sections:
            if raw_size and base <= rva and rva + size <= base + raw_size:
                return raw + rva - base
        raise ValueError(f'address {rva:#x} ({size} bytes) is not in the file')

    def entry(index):
        return entries[index] if len(entries) > index else (0, 0)

    imports = []
    rva, size = entry(IMPORT)
    at = offset(rva, size) if rva and size else None
    while at is not None and at + 20 <= offset(rva, size) + size and _u32(data, at + 12):
        start = offset(_u32(data, at + 12), 1)
        imports.append(data[start:data.index(b'\0', start)].decode('ascii', 'replace').lower())
        at += 20

    resource_types, manifest, version = [], None, {'file': None, 'product': None, 'strings': {}, 'translation': None}
    base, size = entry(RESOURCE)
    if base and size:
        def walk(directory, path):                              # type -> id -> language -> data
            nonlocal version, manifest
            at = offset(directory, 16)
            for i in range(_u16(data, at + 12) + _u16(data, at + 14)):
                name, target = struct.unpack_from('<II', data, at + 16 + 8 * i)
                if not path:
                    resource_types.append(name)                 # the first level is the resource type
                if target & 0x80000000:
                    walk(base + (target & 0x7FFFFFFF), path + [name])
                    continue
                data_rva, length = struct.unpack_from('<II', data, offset(base + target, 16))
                start = offset(data_rva, length)
                blob = data[start:start + length]
                if path[0] == RT_VERSION and version['file'] is None:
                    version = parse_version(blob)
                elif path[0] == RT_MANIFEST and manifest is None:
                    manifest = blob
        walk(base, [])

    certificate = {'offset': entry(CERTIFICATE)[0], 'size': entry(CERTIFICATE)[1]}
    return {
        'machine': machine, 'plus': plus, 'subsystem': _u16(data, optional + 68), 'characteristics': characteristics,
        'dll_characteristics': _u16(data, optional + 70), 'entry_rva': _u32(data, optional + 16),
        'image_size': _u32(data, optional + 56), 'timestamp': _u32(data, pe + 8),
        'linker_version': (data[optional + 2], data[optional + 3]), 'sections': sections,
        'pe_size': image_end, 'container_size': max(0, len(data) - image_end - certificate['size']),
        'certificate': certificate, 'imports': imports, 'resource_types': resource_types,
        'version': version, 'manifest': manifest,
    }


def program_problems(info, version=None):
    """Why the program described by `info` (from pe_info) is not the setup program, whatever follows
    it in the file. `version` is the expected version text, '2.5.0'."""
    bad = []
    if info['machine'] != X64 or not info['plus']:
        bad.append('not a 64-bit program')
    if info['subsystem'] != GUI:
        bad.append(f'the subsystem is {info["subsystem"]}, not 2 (Windows, graphical)')
    extra = sorted(set(info['imports']) - LAUNCHER_IMPORTS)
    if extra:
        bad.append('imports from ' + ', '.join(extra))
    missing = [kind for kind in ('icon', 'icon group', 'version', 'manifest')
               if {'icon': RT_ICON, 'icon group': RT_GROUP_ICON, 'version': RT_VERSION, 'manifest': RT_MANIFEST}[kind]
               not in info['resource_types']]
    if missing:
        bad.append('the file has no ' + ', '.join(missing) + ' resource')
    if version:
        want = tuple(int(part) for part in version.split('.')) + (0,)
        for field in ('file', 'product'):
            if info['version'][field] != want:
                bad.append(f'the {field} version is {info["version"][field]}, not {want}')
        for name in ('FileVersion', 'ProductVersion'):
            if info['version']['strings'].get(name) != version:
                bad.append(f'{name} is {info["version"]["strings"].get(name)!r}, not {version!r}')
    if b'asInvoker' not in (info['manifest'] or b''):
        bad.append('the manifest does not request asInvoker')
    return bad


def launcher_problems(data, version=None):
    """Why `data` cannot be the program part of Setup.exe. `version` is the expected text, '2.5.0'."""
    info = pe_info(data)
    bad = program_problems(info, version)
    if info['certificate']['size']:
        bad.append('the image is already signed (the installer is signed after the container is appended)')
    if info['container_size']:
        bad.append(f'{info["container_size"]} bytes follow the image')
    return bad
