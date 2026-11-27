"""The single-file container: round trip, integrity checks, safe names, deterministic output,
and recognition of signed files (the footer is found through the PE header; the zeros a signing
tool inserts before its 8-byte-aligned certificate table are the only bytes tolerated after it)."""
import hashlib
import shutil
import struct
import unittest

from installer_support import PE_SIZE, fake_pe, fake_sign
from support import tmpdir

import payload_container as pc

NOT_INSTALLER = 'not a Brave Music Downloader installer'
CERT_ENTRY = 0x58 + 112 + 4 * 8                   # Certificate Table entry (data directory 4) of the PE32+ header from fake_pe()


class ContainerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.pe = fake_pe()
        self.entries = [('Setup.ps1', b'param($x)\nWrite-Host hi\n', True),
                        ('Application.zip', bytes(range(256)) * 40, True),
                        ('ffmpeg.bin', b'\x01\x02\x03' * 5000, False)]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, path=None, entries=None, pe=None):
        data = pc.build(pe or self.pe, entries or self.entries)
        if path:
            from pathlib import Path
            Path(path).write_bytes(data)
        return data

    def variant(self, edit, index=1):
        """A copy of the standard file with one index field edited and the index hash refreshed."""
        data = bytearray(self.build())
        parsed = pc.parse(bytes(data))
        edit(data, parsed['index_offset'] + pc.ENTRY.size * index)
        start, length = parsed['index_offset'], parsed['entry_count'] * pc.ENTRY.size
        data[-32:] = hashlib.sha256(data[start:start + length]).digest()
        return bytes(data)

    def test_round_trip_preserves_every_entry_byte_for_byte(self):
        path = self.tmp / 'Setup.exe'
        data = self.build(path)
        parsed = pc.read(path)
        self.assertEqual((parsed['pe_size'], parsed['entry_count'], parsed['data_end'], parsed['signed']),
                         (len(self.pe), 3, len(data), False))
        self.assertEqual([e['name'] for e in parsed['entries']], ['Setup.ps1', 'Application.zip', 'ffmpeg.bin'])
        self.assertEqual([e['bootstrap'] for e in parsed['entries']], [True, True, False])
        self.assertEqual(path.read_bytes()[:len(self.pe)], self.pe)          # the PE image is untouched
        for entry, (name, content, _) in zip(parsed['entries'], self.entries):
            self.assertEqual((pc.entry_data(path, entry), entry['size']), (content, len(content)))

    def test_two_builds_of_the_same_input_are_identical(self):
        self.assertEqual(self.build(), self.build())

    def test_entries_start_on_a_16_byte_boundary_after_the_pe_image(self):
        parsed = pc.parse(self.build())
        for entry in parsed['entries']:
            self.assertEqual(entry['offset'] % 16, 0)
            self.assertGreaterEqual(entry['offset'], len(self.pe))

    def test_a_truncated_or_oversized_file_is_rejected(self):
        data = self.build()
        for cut in (0, 10, 80, len(data) - 1, len(data) - 71):
            with self.assertRaises(pc.ContainerError, msg=cut):
                pc.parse(data[:cut])
        with self.assertRaisesRegex(pc.ContainerError, NOT_INSTALLER):
            pc.parse(data + b'\0' * 4096)               # far more than the alignment padding
        with self.assertRaisesRegex(pc.ContainerError, 'extra data'):
            pc.parse(data + b'x')                       # anything but zeros after the footer
        with self.assertRaises(pc.ContainerError):
            pc.parse(b'')

    def test_only_the_zero_padding_of_a_signing_tool_is_tolerated_after_the_footer(self):
        entries = [('Setup.ps1', b'param($x)\nWrite-Host hi\n', True),
                   ('Application.zip', bytes(range(256)) * 40, True),
                   ('ffmpeg.bin', b'\x01\x02\x03' * 5000 + b'!', False)]   # one byte more: no multiple of 8
        odd = pc.build(self.pe, entries)
        self.assertEqual(len(odd) % 8, 1)
        plain = pc.parse(odd)
        for count in range(1, 8):                       # the zeros a signing tool inserts, unsigned file
            padded = odd + bytes(count)
            parsed = pc.parse(padded)
            self.assertEqual((parsed['signed'], parsed['data_end']), (False, len(odd)), count)
            self.assertEqual(parsed['entries'], plain['entries'])
        with self.assertRaisesRegex(pc.ContainerError, NOT_INSTALLER):
            pc.parse(odd + bytes(8))                    # eight: too many for an 8-byte alignment
        with self.assertRaisesRegex(pc.ContainerError, 'extra data'):
            pc.parse(odd + b'\0' * 6 + b'\x01')
        signed = fake_sign(odd)                         # pads with 7 zeros, then appends its table
        self.assertEqual(len(signed) - len(odd), 7 + 256 + 8)
        path = self.tmp / 'Setup-signed-odd.exe'
        path.write_bytes(signed)
        parsed = pc.read(path)
        self.assertEqual((parsed['signed'], parsed['data_end'], parsed['index_offset']),
                         (True, len(odd), plain['index_offset']))
        self.assertEqual(parsed['entries'], plain['entries'])
        for entry, (_, content, _) in zip(parsed['entries'], entries):
            self.assertEqual(pc.entry_data(path, entry), content)
        spoiled = bytearray(signed)
        spoiled[len(odd) + 3] = 0xFF                    # inside the padding
        path.write_bytes(spoiled)
        with self.assertRaisesRegex(pc.ContainerError, 'extra data'):
            pc.read(path)
        spoiled = bytearray(signed)
        struct.pack_into('<II', spoiled, CERT_ENTRY, len(spoiled) - 255, 256)   # the table would run past the end
        path.write_bytes(spoiled)
        with self.assertRaisesRegex(pc.ContainerError, 'certificate table lies outside'):
            pc.read(path)

    def test_a_file_that_is_not_an_installer_is_rejected(self):
        with self.assertRaisesRegex(pc.ContainerError, NOT_INSTALLER):
            pc.parse(b'random file content' * 20)
        with self.assertRaisesRegex(pc.ContainerError, NOT_INSTALLER):
            pc.parse(self.pe)                           # an executable without a container

    def test_every_single_byte_flip_in_the_index_is_detected(self):
        data = bytearray(self.build())
        index_offset = pc.parse(bytes(data))['index_offset']
        for position in range(index_offset, len(data) - pc.FOOTER.size):
            damaged = bytearray(data)
            damaged[position] ^= 0x5A
            with self.assertRaisesRegex(pc.ContainerError, 'damaged', msg=f'index byte {position}'):
                pc.parse(bytes(damaged))

    def test_every_single_byte_flip_in_the_footer_is_detected(self):
        data = bytearray(self.build())
        start = len(data) - pc.FOOTER.size
        for position in range(start, len(data)):
            damaged = bytearray(data)
            damaged[position] ^= 0x5A
            if position - start < 8:
                self.assertRaisesRegex(pc.ContainerError, NOT_INSTALLER, pc.parse, bytes(damaged))
            else:
                with self.assertRaises(pc.ContainerError, msg=f'footer byte {position - start}') as caught:
                    pc.parse(bytes(damaged))
                self.assertNotIn(NOT_INSTALLER, str(caught.exception))

    def test_an_entry_pointing_out_of_its_bounds_is_rejected(self):
        for value in (1, PE_SIZE - 1, 2 ** 63 - 1):
            with self.assertRaises(pc.ContainerError, msg=value):
                pc.parse(self.variant(lambda d, at: struct.pack_into('<Q', d, at + 64, value)))
        for value in (pc.parse(self.build())['index_offset'], 2 ** 64 - 1):
            with self.assertRaises(pc.ContainerError, msg=value):
                pc.parse(self.variant(lambda d, at: struct.pack_into('<Q', d, at + 72, value)))

    def test_entry_fields_are_validated_even_when_the_index_hash_matches(self):
        def rename(data, at, raw):
            data[at:at + 64] = raw.ljust(64, b'\0')

        cases = {
            'duplicate ignoring case': lambda d, at: rename(d, at, b'setup.ps1'),
            'bytes after the name': lambda d, at: d.__setitem__(at + 20, ord('x')),
            'reserved field': lambda d, at: struct.pack_into('<I', d, at + 84, 7),
            'unknown flag': lambda d, at: struct.pack_into('<I', d, at + 80, 3),
            'empty name': lambda d, at: rename(d, at, b''),
            'name with a trailing dot': lambda d, at: rename(d, at, b'x.'),
            'name with a trailing space': lambda d, at: rename(d, at, b'x '),
            '49-character name': lambda d, at: rename(d, at, b'a' * 49),
            'non-ascii name': lambda d, at: rename(d, at, 'caf\xe9'.encode('utf-8')),
            'device name': lambda d, at: rename(d, at, b'NUL'),
        }
        for label, edit in cases.items():
            with self.assertRaisesRegex(pc.ContainerError, 'damaged', msg=label):
                pc.parse(self.variant(edit))
        flagged = pc.parse(self.variant(lambda d, at: struct.pack_into('<I', d, at + 80, pc.BOOTSTRAP)))
        self.assertEqual([e['bootstrap'] for e in flagged['entries']], [True, True, False])

    def test_unsafe_entry_names_are_refused_at_build_time(self):
        for name in ('../x', 'a/b', 'a\\b', 'C:x', '', '.Hidden', 'x' * 64, 'tab\tname', 'nul\x00x',
                     'trailing.', 'trailing ', 'x' * 49, 'caf\xe9',
                     'CON', 'nul', 'Aux.txt', 'com1', 'LPT9.log', 'CON .txt', 'conin$', 'conout$', 'com1.txt.bak'):
            with self.assertRaises(ValueError, msg=repr(name)):
                pc.build(self.pe, [(name, b'x', False)])
        for name in ('console', 'COM10', 'auxiliary', 'prune'):          # only exact device names are refused
            pc.build(self.pe, [(name, b'x', False)])
        self.assertEqual(pc.parse(pc.build(self.pe, [('a' * 48, b'x', False)]))['entries'][0]['name'], 'a' * 48)

    def test_duplicate_names_and_empty_or_oversized_containers_are_refused(self):
        with self.assertRaises(ValueError):
            pc.build(self.pe, [('a', b'1', False), ('A', b'2', False)])
        with self.assertRaises(ValueError):
            pc.build(self.pe, [])
        with self.assertRaises(ValueError):
            pc.build(self.pe, [(f'e{i}', b'x', False) for i in range(pc.MAX_ENTRIES + 1)])

    def test_a_signed_installer_is_recognised_and_reads_identically(self):
        data = self.build()
        signed = fake_sign(data)
        path = self.tmp / 'Setup-signed.exe'
        path.write_bytes(signed)
        parsed, plain = pc.read(path), pc.parse(data)
        self.assertIn(pc.MAGIC, signed[len(data):])        # by construction: the footer must not be searched for
        self.assertEqual((parsed['signed'], plain['signed'], parsed['data_end']), (True, False, len(data)))
        for key in ('pe_size', 'index_offset', 'entry_count', 'entries'):
            self.assertEqual(parsed[key], plain[key])
        for entry, (_, content, _) in zip(parsed['entries'], self.entries):
            self.assertEqual(pc.entry_data(path, entry), content)
        target = self.tmp / 'out' / 'ffmpeg.bin'
        pc.copy_entry(path, parsed['entries'][2], target, parsed['entries'][2]['sha256'])
        self.assertEqual(target.read_bytes(), self.entries[2][1])
        self.assertEqual(pc.parse(fake_sign(data, table_bytes=8))['entries'], plain['entries'])

    def test_the_certificate_table_must_sit_after_the_footer(self):
        def pointing(offset, size):
            data = bytearray(self.build())
            struct.pack_into('<II', data, CERT_ENTRY, offset, size)
            return bytes(data)

        for offset, size in ((300, 100), (PE_SIZE, 8), (10_000_000, 64), (PE_SIZE, 0x100000), (0, 10_000_000)):
            with self.assertRaises(pc.ContainerError, msg=(offset, size)):
                pc.parse(pointing(offset, size))
        with self.assertRaisesRegex(pc.ContainerError, NOT_INSTALLER):
            pc.parse(pointing(300, 100))
        with self.assertRaisesRegex(pc.ContainerError, 'certificate table lies outside'):
            pc.parse(pointing(10_000_000, 64))
        self.assertEqual(pc.parse(pointing(7777, 0))['entry_count'], 3)      # size 0: not signed

    def test_without_a_windows_executable_header_nothing_is_built_or_parsed(self):
        for image in (b'', b'plain text, not a program' * 40, b'MZ' + bytes(range(256)) * 3, b'PE\0\0' + self.pe[4:]):
            with self.assertRaisesRegex(pc.ContainerError, 'no Windows executable header'):
                pc.build(image, self.entries)
        with self.assertRaisesRegex(pc.ContainerError, 'no Windows executable header'):
            pc.parse(b'nope' + self.build()[4:])

    def test_pe32_headers_are_accepted_too(self):
        path = self.tmp / 'Setup32.exe'
        self.build(path, pe=fake_pe(magic=0x10B))
        self.assertEqual([e['name'] for e in pc.read(path)['entries']], ['Setup.ps1', 'Application.zip', 'ffmpeg.bin'])
        pe32 = bytearray(self.build(pe=fake_pe(magic=0x10B)))
        struct.pack_into('<II', pe32, 0x58 + 96 + 4 * 8, len(pe32), 40)
        parsed = pc.parse(bytes(pe32) + bytes(40))
        self.assertTrue(parsed['signed'])                 # a table announced 16 bytes earlier

    def test_an_already_signed_image_is_refused_by_the_build(self):
        signed_image = fake_pe(cert_offset=PE_SIZE, cert_size=8) + bytes(8)
        with self.assertRaisesRegex(ValueError, 'already signed'):
            pc.build(signed_image, self.entries)          # sign the finished installer, so the signature covers it

    def test_header_fields_out_of_range_are_rejected(self):
        for offset, fmt, value, expected in ((0x3c, '<I', 0xFFFFFFF0, 'no Windows executable header'),
                                             (0x58, '<H', 0x1234, 'no Windows executable header'),
                                             (0x58 + 108, '<I', 4, NOT_INSTALLER)):
            data = bytearray(self.build())
            struct.pack_into(fmt, data, offset, value)
            data = bytes(data)
            if value == 4:                                # a certificate table the header does not announce
                data = fake_sign(data)
            with self.assertRaisesRegex(pc.ContainerError, expected, msg=(offset, value)):
                pc.parse(data)

    def test_copy_entry_verifies_the_content_and_leaves_nothing_behind_when_it_does_not_match(self):
        path = self.tmp / 'Setup.exe'
        self.build(path)
        entry = pc.by_name(pc.read(path))['ffmpeg.bin']
        seen = []
        target = self.tmp / 'cache' / 'ffmpeg.bin'
        digest = pc.copy_entry(path, entry, target, entry['sha256'], report=lambda done, total: seen.append((done, total)))
        self.assertEqual(digest, entry['sha256'])
        self.assertEqual((target.read_bytes(), seen[-1]), (self.entries[2][1], (15000, 15000)))
        self.assertFalse(target.with_name('ffmpeg.bin.partial').exists())

        raw = bytearray(path.read_bytes())
        raw[entry['offset'] + 100] ^= 0xFF                 # damage the entry's data
        path.write_bytes(raw)
        with self.assertRaisesRegex(pc.ContainerError, 'does not match its SHA-256'):
            pc.copy_entry(path, entry, target, entry['sha256'])
        self.assertFalse(target.with_name('ffmpeg.bin.partial').exists())

    def test_copy_entry_honours_an_additional_expected_hash(self):
        path = self.tmp / 'Setup.exe'
        self.build(path)
        entry = pc.by_name(pc.read(path))['ffmpeg.bin']
        with self.assertRaisesRegex(pc.ContainerError, 'version pinned for this release'):
            pc.copy_entry(path, entry, self.tmp / 'out.bin', expected='0' * 64)
        self.assertFalse((self.tmp / 'out.bin').exists())
        self.assertFalse((self.tmp / 'out.bin.partial').exists())


if __name__ == '__main__':
    unittest.main()
