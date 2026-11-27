"""The release build (scripts/build_release.py): its gates, the installer it assembles, and the lock
refresh (scripts/refresh_tool_lock.py). Nothing here needs the network: builds are online-only or
use fake tool archives, and the lock refresh talks to a fake web."""
import contextlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import unittest
import urllib.error
import zipfile
from email.message import Message
from pathlib import Path
from unittest import mock

from support import ROOT, tmpdir
from installer_support import EXT_ID, Server, fake_components, fake_sign, sha

sys.path.insert(0, str(ROOT / 'scripts'))
import build_launcher  # noqa: E402
import build_release as br  # noqa: E402
import installer_core as ic  # noqa: E402
import payload_container as pc  # noqa: E402
import refresh_tool_lock as rl  # noqa: E402
import sigstore_verify as sv  # noqa: E402
from pe_tools import GUI, X64, launcher_problems, pe_info  # noqa: E402

VERSION = (ROOT / 'VERSION').read_text().strip()
CACHE = ROOT / '.release-cache'
try:
    ZIG = build_launcher.find_zig()
except build_launcher.BuildError:
    ZIG = None


def copy_source(dest):
    shutil.copytree(ROOT, dest, ignore=shutil.ignore_patterns(
        '__pycache__', 'dist', '.git', 'node_modules', '.zig-cache', '.release-cache', '.cache'))
    return dest


def source_of(source, name):
    """The file in `source` that was packed into the archive entry `name` (build_release.PAYLOAD)."""
    for src, dest in br.PAYLOAD:
        base = source / src
        if base.is_file() and name == dest:
            return base
        if not base.is_file() and name.startswith(dest + '/'):
            return base / name[len(dest) + 1:]
    raise AssertionError('no payload item packs ' + name)


def refuse(request, timeout=0):
    raise AssertionError('a test tried to use the network: ' + request.full_url)


def pe_machine(data):
    """The machine field of a Windows program's header (None when the bytes are not one)."""
    at = struct.unpack_from('<I', data, 0x3c)[0] if data[:2] == b'MZ' and len(data) > 0x40 else -1
    return struct.unpack_from('<H', data, at + 4)[0] if at >= 0 and data[at:at + 4] == b'PE\0\0' else None


class FakeResponse(io.BytesIO):
    status = 200

    def __init__(self, body):
        super().__init__(body)
        self.headers = {'Content-Length': str(len(body))}


class FakeHttp:
    """Answers Request objects from a route table. It serves both roles the lock refresh gives the
    network: an opener for downloads and the redirect probe for GitHub's latest-release links."""

    def __init__(self, routes, redirects):
        self.routes, self.redirects, self.hits = routes, redirects, []

    def open(self, request, timeout=60):
        url = request.full_url
        self.hits.append(url)
        if request.get_method() == 'HEAD':
            if url not in self.redirects:
                raise urllib.error.HTTPError(url, 404, 'Not Found', None, None)
            headers = Message()
            headers['Location'] = self.redirects[url]
            raise urllib.error.HTTPError(url, 302, 'Found', headers, None)
        if url not in self.routes:
            raise urllib.error.HTTPError(url, 404, 'Not Found', None, None)
        return FakeResponse(self.routes[url])

    __call__ = open


def tree_lock():
    return json.loads((ROOT / 'tools.lock.json').read_text(encoding='utf-8'))


# ------------------------------------------------------------------ the release build ----

@unittest.skipIf(ZIG is None, 'Zig 0.14 is not installed (set ZIG=/path/to/zig)')
class ReleaseBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tmpdir()
        cls.out = cls.tmp / 'dist'
        cls.src = copy_source(cls.tmp / 'src')
        assert br.main(['--source', str(cls.src), '--out', str(cls.out), '--online-only', '--zig', ZIG]) == 0

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def installer(self, folder=None):
        folder = folder or self.out
        data = (folder / br.ONLINE).read_bytes()
        return data, pe_info(data), pc.parse(data)

    def test_outputs_exist_and_checksums_match(self):
        names = ('Application.zip', br.EXTENSION_ZIP.format(version=VERSION), 'Setup.ps1', 'SHA256SUMS.txt',
                 'release.json', br.ONLINE)
        for name in names:
            self.assertTrue((self.out / name).is_file(), name)
        self.assertFalse((self.out / br.OFFLINE).exists(), '--online-only builds no offline installer')
        listed = sorted(set(names) - {'SHA256SUMS.txt', 'release.json'})      # a file cannot hold its own hash
        self.assertEqual(sorted(json.loads((self.out / 'release.json').read_text())['files']), listed)
        for line in (self.out / 'SHA256SUMS.txt').read_text().splitlines():
            digest, name = line.split('  ')
            self.assertEqual(sha((self.out / name).read_bytes()), digest, name)
        self.assertEqual(sorted(line.split('  ')[1] for line in (self.out / 'SHA256SUMS.txt').read_text().splitlines()),
                         listed)
        info = json.loads((self.out / 'release.json').read_text())
        self.assertEqual((info['version'], info['extension_id']), (VERSION, EXT_ID))
        self.assertEqual(sorted(info['installers']), [br.ONLINE])
        self.assertFalse(info['signed'])
        self.assertFalse(info['installers'][br.ONLINE]['signed'])       # said plainly, not implied
        self.assertIn('signtool', info['signature_note'])
        self.assertEqual(info['zig'], subprocess.run([ZIG, 'version'], capture_output=True, text=True).stdout.strip())

    def test_installer_is_the_setup_program_followed_by_its_payload(self):
        data, info, container = self.installer()
        self.assertEqual((info['machine'], info['plus'], info['subsystem']), (X64, True, GUI))
        self.assertEqual(info['certificate'], {'offset': 0, 'size': 0})           # not signed
        self.assertEqual(info['version']['file'], tuple(int(part) for part in VERSION.split('.')) + (0,))
        self.assertEqual(info['version']['strings']['FileVersion'], VERSION)
        self.assertEqual(container['pe_size'], info['pe_size'])
        self.assertFalse(container['signed'])
        self.assertEqual([(entry['name'], entry['bootstrap']) for entry in container['entries']],
                         [('Setup.ps1', True), ('Application.zip', True)])
        path = self.out / br.ONLINE
        entries = pc.by_name(container)
        self.assertEqual(pc.entry_data(path, entries['Setup.ps1']), (ROOT / 'Setup.ps1').read_bytes())
        self.assertEqual(pc.entry_data(path, entries['Application.zip']), (self.out / 'Application.zip').read_bytes())
        for entry in container['entries']:
            self.assertEqual(entry['sha256'], sha(pc.entry_data(path, entry)), entry['name'])
        image, launcher = data[:container['pe_size']], json.loads((self.out / 'release.json').read_text())['launcher']
        self.assertEqual(launcher_problems(image, VERSION), [])
        self.assertEqual((launcher['bytes'], launcher['sha256']), (len(image), sha(image)))

    def test_build_is_reproducible(self):
        again = self.tmp / 'again'
        self.assertEqual(br.main(['--source', str(self.src), '--out', str(again), '--online-only', '--zig', ZIG]), 0)
        for name in (br.ONLINE, 'Application.zip', br.EXTENSION_ZIP.format(version=VERSION), 'Setup.ps1',
                     'SHA256SUMS.txt'):
            self.assertEqual((again / name).read_bytes(), (self.out / name).read_bytes(), name)

    def test_payload_is_complete_sorted_and_free_of_binaries(self):
        with zipfile.ZipFile(self.out / 'Application.zip') as bundle:
            names, stored = bundle.namelist(), {name: bundle.read(name) for name in bundle.namelist()}
        self.assertEqual(names, sorted(names))
        self.assertFalse([n for n in names if Path(n).suffix.lower() in br.FORBIDDEN_SUFFIXES
                          or set(Path(n).parts) & br.FORBIDDEN_DIRS])
        for required in ic.REQUIRED_PAYLOAD:
            self.assertIn(required, names)
        self.assertEqual(sorted({name.split('/')[0] for name in names}),
                         ['README.md', 'Uninstall.ps1', 'bridge', 'extension', 'helper', 'installer', 'tools.lock.json'])
        self.assertFalse([n for n in names if n.startswith(('tests', 'scripts', 'docs', 'bench', 'installer/launcher/'))],
                         'the setup program\'s own sources are compiled in, not installed')
        self.assertIn('bridge/setup.ico', names)
        self.assertEqual(stored['tools.lock.json'], (ROOT / 'tools.lock.json').read_bytes())
        self.assertNotIn('Setup.ps1', names)                    # Setup.ps1 travels next to the zip
        for name in names:                                      # every file is exactly the one in the tree
            self.assertEqual(stored[name], source_of(self.src, name).read_bytes(), name)

    def test_extension_archive_is_the_extension_folder_with_its_manifest_at_the_root(self):
        """The archive an extension store takes, or a user unpacks to load the extension by hand."""
        name = br.EXTENSION_ZIP.format(version=VERSION)
        folder = self.src / 'app' / 'extension'
        expected = sorted(p.relative_to(folder).as_posix() for p in folder.rglob('*') if p.is_file())
        with zipfile.ZipFile(self.out / name) as bundle, zipfile.ZipFile(self.out / 'Application.zip') as payload:
            self.assertIsNone(bundle.testzip())
            self.assertEqual(bundle.namelist(), expected)
            self.assertEqual(bundle.namelist(), sorted(bundle.namelist()))
            self.assertIn('manifest.json', bundle.namelist())    # at the root, not inside a folder
            self.assertTrue(all(not n.startswith('/') and '..' not in Path(n).parts for n in expected))
            manifest = json.loads(bundle.read('manifest.json'))
            self.assertEqual((manifest['version'], manifest['manifest_version']), (VERSION, 3))
            self.assertFalse([n for n in expected if Path(n).suffix.lower() in br.FORBIDDEN_SUFFIXES
                              or set(Path(n).parts) & br.FORBIDDEN_DIRS])
            for entry in expected:                              # the same bytes the installer hands out
                self.assertEqual(bundle.read(entry), payload.read('extension/' + entry), entry)
        release = json.loads((self.out / 'release.json').read_text())
        self.assertEqual(release['extension'], {'file': name, 'bytes': (self.out / name).stat().st_size,
                                                'sha256': sha((self.out / name).read_bytes()), 'files': len(expected)})

    def test_signing_does_not_hide_the_container(self):
        """What signtool does to the file: a certificate table at the end that contains the magic."""
        data, _, unsigned = self.installer()
        signed = fake_sign(data)
        path = self.tmp / 'signed.exe'
        path.write_bytes(signed)
        container = pc.read(path)
        self.assertTrue(container['signed'])
        self.assertEqual(container['pe_size'], unsigned['pe_size'])
        for before, after in zip(unsigned['entries'], container['entries']):
            self.assertEqual((before['name'], before['offset'], before['size']), (after['name'], after['offset'], after['size']))
            self.assertEqual(pc.entry_data(path, after), pc.entry_data(self.out / br.ONLINE, before))
        problems = br.installer_problems(signed, VERSION, [(e['name'], pc.entry_data(self.out / br.ONLINE, e), e['bootstrap'])
                                                           for e in unsigned['entries']])
        self.assertEqual(problems, ['the file is signed; a fresh build is not (signing is the last step)'])

    def test_stale_outputs_of_earlier_layouts_are_removed(self):
        again = self.tmp / 'stale'
        again.mkdir()
        old_extension = br.EXTENSION_ZIP.format(version='0.0.1')
        for name in (*br.STALE_OUTPUTS, br.OFFLINE, old_extension, 'notes.txt'):
            (again / name).write_bytes(b'old')
        self.assertEqual(br.main(['--source', str(self.src), '--out', str(again), '--online-only', '--zig', ZIG]), 0)
        for name in (*br.STALE_OUTPUTS, br.OFFLINE, old_extension):
            self.assertFalse((again / name).exists(), name)
        self.assertTrue((again / br.EXTENSION_ZIP.format(version=VERSION)).is_file())
        self.assertTrue((again / 'notes.txt').is_file(), 'unrelated files are left alone')

    def test_real_payload_installs_runs_its_host_and_uninstalls(self):
        server = Server()
        try:
            layout = ic.Layout(self.tmp / 'e2e' / 'Local' / 'BMD', self.tmp / 'e2e' / 'Roaming' / 'BMD',
                               self.tmp / 'e2e' / 'Music')
            registry = ic.FakeRegistry()
            manifest = ic.install(layout, self.out / 'Application.zip', fake_components(server), registry,
                                  extension_id=EXT_ID, rename_retries=1)
            self.assertEqual(manifest['version'], VERSION)
            host = layout.app / 'helper' / 'native_host.py'
            env = dict(os.environ, BMD_APP_ROOT=str(layout.app), BMD_INSTALL_ROOT=str(layout.root),
                       BMD_DATA_DIR=str(layout.data_dir), BMD_OUTPUT=str(layout.output), PYTHONDONTWRITEBYTECODE='1')

            def frame(obj):
                raw = json.dumps(obj).encode()
                return struct.pack('<I', len(raw)) + raw

            request = frame({'id': 1, 'method': 'health'}) + frame({'id': 2, 'method': 'jobs', 'params': {'limit': 5}})
            run = subprocess.run([sys.executable, str(host)], input=request, capture_output=True, env=env, timeout=60)
            self.assertEqual(run.returncode, 0, run.stderr.decode())
            data, replies = run.stdout, []
            while data:                      # stdout must contain nothing but framed JSON
                (n,) = struct.unpack('<I', data[:4])
                replies.append(json.loads(data[4:4 + n]))
                data = data[4 + n:]
            self.assertEqual([reply['id'] for reply in replies], [1, 2])
            self.assertEqual(replies[0]['data']['version'], VERSION)
            self.assertEqual(replies[1]['data']['jobs'], [])
            self.assertTrue((layout.data_dir / 'queue.sqlite3').exists())
            report = ic.uninstall(layout, registry)
            self.assertEqual(report['failed'], [])
            self.assertFalse(layout.app.exists())
            self.assertTrue((layout.data_dir / 'queue.sqlite3').exists(), 'history survives uninstall')
            self.assertEqual(registry.keys, {})
        finally:
            server.close()


# --------------------------------------------------------- the offline installer's tools ----

class OfflineBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.cache = self.tmp / 'cache'
        self.blob = {name: (name.encode() * 1000) for name in br.BUNDLED}
        self.lock = {'components': [
            {'name': name, 'version': '9.9', 'url': f'https://example.invalid/{name}.zip', 'sha256': sha(blob),
             'provides': [f'tools/{name}.exe']} for name, blob in self.blob.items()]}
        self.http = FakeHttp({entry['url']: self.blob[entry['name']] for entry in self.lock['components']}, {})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bundled_downloads_each_pinned_tool_once_and_keeps_it(self):
        result = br.bundled(self.lock, self.cache, self.http)
        self.assertEqual(result, [(name, self.blob[name], name == 'python') for name in br.BUNDLED])
        for name in br.BUNDLED:
            kept = br.cache_file(name, sha(self.blob[name]), self.cache)
            self.assertEqual(kept.read_bytes(), self.blob[name])
        first = len(self.http.hits)
        self.assertEqual(br.bundled(self.lock, self.cache, self.http), result)
        self.assertEqual(len(self.http.hits), first, 'the second build downloads nothing')

    def test_unpinned_or_missing_components_are_refused(self):
        self.lock['components'][2].pop('sha256')
        with self.assertRaisesRegex(br.ReleaseError, 'deno is not pinned.*--online-only'):
            br.bundled(self.lock, self.cache, self.http)
        self.lock['components'] = self.lock['components'][:2]
        with self.assertRaisesRegex(br.ReleaseError, 'tools.lock.json has no deno, ffmpeg'):
            br.bundled(self.lock, self.cache, self.http)

    def test_download_that_does_not_match_the_pin_is_refused(self):
        self.http.routes[self.lock['components'][1]['url']] = b'tampered'
        with self.assertRaisesRegex(br.ReleaseError, 'Checksum mismatch'):
            br.bundled(self.lock, self.cache, self.http)
        self.assertFalse(br.cache_file('yt-dlp', sha(self.blob['yt-dlp']), self.cache).exists())


@unittest.skipIf(ZIG is None, 'Zig 0.14 is not installed (set ZIG=/path/to/zig)')
class OfflineInstallerBuildTests(unittest.TestCase):
    def test_offline_installer_carries_the_tools_after_the_bootstraps(self):
        tmp = tmpdir()
        try:
            cache, out, src = tmp / 'cache', tmp / 'dist', copy_source(tmp / 'src')
            blob = {name: (name.encode() * 1000) for name in br.BUNDLED}
            lock = {'components': [{'name': name, 'version': '9.9', 'url': f'https://example.invalid/{name}.zip',
                                    'sha256': sha(data), 'provides': [f'tools/{name}.exe']}
                                   for name, data in blob.items()]}
            http = FakeHttp({entry['url']: blob[entry['name']] for entry in lock['components']}, {})
            facts = {'version': VERSION, 'extension_id': EXT_ID, 'tools': {}, 'release_gate': 'test'}
            release = br.build(src, out, facts, lock, zig=ZIG, cache=cache, opener=http)
            container = pc.read(out / br.OFFLINE)
            self.assertEqual([(entry['name'], entry['bootstrap']) for entry in container['entries']],
                             [('Setup.ps1', True), ('Application.zip', True), ('python', True),
                              ('yt-dlp', False), ('deno', False), ('ffmpeg', False)])
            for entry, name in zip(container['entries'][2:], br.BUNDLED):
                self.assertEqual(pc.entry_data(out / br.OFFLINE, entry), blob[name], name)
            online = release['installers'][br.ONLINE]
            self.assertEqual(online['bytes'], (out / br.ONLINE).stat().st_size)
            self.assertEqual(release['installers'][br.OFFLINE]['entries'][2:],
                             [{'name': name, 'bytes': len(blob[name]), 'sha256': sha(blob[name]),
                               'unpacked_by_launcher': name == 'python'} for name in br.BUNDLED])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class PinnedLockTests(unittest.TestCase):
    """The lock in the tree: pinned, and (when the release cache is there) really installable."""

    def test_lock_is_pinned_and_says_how_each_hash_was_cross_checked(self):
        summary = br.check_lock(ROOT, require_pinned=True)
        lock = tree_lock()
        self.assertEqual(summary, {entry['name']: 'pinned' for entry in lock['components']})
        self.assertEqual(sorted(br.BUNDLED), sorted(summary))
        evidence = lock['pinned']['evidence']
        for entry in lock['components']:
            self.assertRegex(entry['sha256'], r'^[0-9a-f]{64}$', entry['name'])
            self.assertNotIn('checksum', entry)
            self.assertTrue(evidence[entry['name']], entry['name'])
            if entry['name'] != 'python':
                self.assertIn('publisher checksum', evidence[entry['name']])
        self.assertIn('Sigstore', evidence['python'])
        self.assertIn(sv.signer(lock['components'][0]['version'])[0], evidence['python'])

    def test_pinned_archives_install_through_the_engine_without_the_network(self):
        lock = tree_lock()
        missing = [entry['name'] for entry in lock['components']
                   if not br.cache_file(entry['name'], entry['sha256'], CACHE).is_file()]
        if missing:
            self.skipTest('no release cache for ' + ', '.join(missing) + ' (scripts/refresh_tool_lock.py fills it)')
        tmp = tmpdir()
        try:
            layout = ic.Layout(tmp / 'Local' / 'BMD', tmp / 'Roaming' / 'BMD', tmp / 'Music')
            layout.downloads.mkdir(parents=True)
            for entry in lock['components']:                 # what the engine's own download step would have kept
                shutil.copyfile(br.cache_file(entry['name'], entry['sha256'], CACHE),
                                layout.downloads / f"{entry['name']}-{entry['sha256'][:16]}.bin")
            staging = tmp / 'staging'
            records, summary = ic.install_components(layout, staging, lock, refuse, False, None, 'sha256', False)
            self.assertEqual(summary, {'reused': [], 'repaired': []})
            by = {record['name']: record for record in records}
            for entry in lock['components']:
                self.assertEqual((by[entry['name']]['verified_by'], by[entry['name']]['source']), ('pinned', 'download'))
                self.assertEqual(by[entry['name']]['sha256'], entry['sha256'])
            for rel in ('python/python.exe', 'tools/yt-dlp.exe', 'tools/deno.exe', 'tools/ffmpeg.exe', 'tools/ffprobe.exe'):
                self.assertTrue((staging / rel).is_file(), rel)
                self.assertEqual(pe_machine((staging / rel).read_bytes()), X64, rel + ' is not a 64-bit Windows program')
            ic.patch_pth(staging / 'python')               # the embedded Python's path file is where the engine expects it
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# -------------------------------------------------------------------- release gates ----

class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.src = copy_source(self.tmp / 'src')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, *extra):
        """(exit code, stderr) of a build of the copied tree."""
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            code = br.main(['--source', str(self.src), '--out', str(self.tmp / 'dist'), *extra])
        return code, out.getvalue()

    def gate(self, *extra):
        """stderr of a build that must refuse the tree: a gate stops before anything is built."""
        code, text = self.build(*extra)
        self.assertEqual(code, 2, text)
        self.assertTrue(text.startswith('RELEASE BLOCKED: '), text)
        return text

    def edit(self, rel, old, new):
        path = self.src / rel
        text = path.read_text(encoding='utf-8')
        self.assertIn(old, text, f'{rel} no longer contains {old!r}: the test would not test this gate')
        path.write_text(text.replace(old, new), encoding='utf-8')

    def write_json(self, rel, value):
        (self.src / rel).write_text(json.dumps(value, indent=2), encoding='utf-8')

    def test_clean_tree_passes_every_gate(self):
        self.assertEqual(br.check_versions(self.src), VERSION)
        self.assertEqual(br.check_identity(self.src), EXT_ID)
        br.check_extension(self.src)
        self.assertEqual(set(br.check_lock(self.src, True).values()), {'pinned'})
        br.check_content(br.collect(self.src), self.src)
        code, text = self.build('--require-pinned', '--online-only', *(['--zig', ZIG] if ZIG else []))
        if ZIG:
            self.assertEqual(code, 0, text)
        else:
            self.assertEqual(code, 1, text)                 # past every gate; only the compiler is missing
            self.assertIn('RELEASE FAILED: Zig was not found', text)

    def test_blocks_build_debris_with_its_location(self):
        (self.src / 'app/helper/__pycache__').mkdir()
        (self.src / 'app/helper/__pycache__/x.pyc').write_text('x')
        self.assertIn('Build debris in payload: app/helper/__pycache__/x.pyc', self.gate())

    def test_blocks_bundled_binaries(self):
        (self.src / 'app/helper/yt-dlp.exe').write_bytes(b'MZ')
        self.assertIn('Binary files must not be bundled: app/helper/yt-dlp.exe', self.gate())

    def test_blocks_a_missing_payload_item(self):
        (self.src / 'app/Uninstall.ps1').unlink()
        self.assertIn('Missing payload item: app/Uninstall.ps1', self.gate())

    def test_every_version_statement_is_checked(self):
        cases = [('extension manifest', 'app/extension/manifest.json', f'"version": "{VERSION}"', '"version": "0.0.1"'),
                 ('installer_core', 'app/installer/installer_core.py', f"VERSION = '{VERSION}'", "VERSION = '0.0.1'"),
                 ('helper', 'app/helper/helper.py', f"VERSION = '{VERSION}'", "VERSION = '0.0.1'"),
                 ('Setup.ps1', 'Setup.ps1', f"$script:Version = '{VERSION}'", "$script:Version = '0.0.1'"),
                 ('setup program', 'app/installer/launcher/setup_version.h',
                  f'#define SETUP_VERSION_TEXT "{VERSION}"', '#define SETUP_VERSION_TEXT "0.0.1"'),
                 ('setup manifest', 'app/installer/launcher/setup.manifest',
                  f'version="{VERSION}.0"', 'version="0.0.1.0"')]
        for label, rel, old, new in cases:
            with self.subTest(statement=label):
                self.edit(rel, old, new)
                self.assertIn(f"'{label}': '0.0.1'", self.gate())
                path = self.src / rel                      # repair before the next statement is tried
                path.write_text(path.read_text(encoding='utf-8').replace(new, old), encoding='utf-8')
        with self.subTest(statement='CHANGELOG'):
            path = self.src / 'CHANGELOG.md'
            path.write_text(path.read_text(encoding='utf-8').replace(f'## {VERSION}', '## 0.0.1', 1), encoding='utf-8')
            self.assertIn("'CHANGELOG': '0.0.1'", self.gate())
            path.write_text((ROOT / 'CHANGELOG.md').read_text(encoding='utf-8'), encoding='utf-8')
        with self.subTest(statement='README'):
            path = self.src / 'app/README.md'
            text = path.read_text(encoding='utf-8')
            self.assertRegex(text, rf'(?:v|version ){VERSION}')
            path.write_text(text.replace(VERSION, '0.0.1', 1), encoding='utf-8')
            self.assertIn("'README': '0.0.1'", self.gate())

    def test_missing_version_statement_is_a_gate_not_a_crash(self):
        (self.src / 'app/installer/launcher/setup.manifest').unlink()
        self.assertIn('app/installer/launcher/setup.manifest: cannot be read', self.gate())

    def test_blocks_permission_creep_and_manifest_v2(self):
        self.edit('app/extension/manifest.json', '"nativeMessaging"', '"nativeMessaging", "cookies"')
        self.assertIn('Extension permissions changed: alarms, contextMenus, cookies, nativeMessaging, notifications, '
                      'storage', self.gate())
        (self.src / 'app/extension/manifest.json').write_bytes((ROOT / 'app/extension/manifest.json').read_bytes())
        self.edit('app/extension/manifest.json', '"manifest_version": 3', '"manifest_version": 2')
        self.assertIn('Extension must be Manifest V3', self.gate())

    def test_blocks_extension_id_mismatch(self):
        (self.src / 'extension_id.txt').write_text('a' * 32)
        self.assertIn('Extension ID differs between manifest key, extension_id.txt and Setup.ps1', self.gate())

    def test_blocks_a_version_written_into_a_page(self):
        # The pages show the installed version from the manifest; the 2.3.0 popup still said v2.0.
        self.edit('app/extension/popup.html', '<span class="version" id="appVersion"></span>',
                  '<span class="version" id="appVersion">v0.0.1</span>')
        self.assertIn('popup.html: the version 0.0.1 is written into the page', self.gate())
        path = self.src / 'app/extension/popup.html'
        path.write_text((ROOT / 'app/extension/popup.html').read_text(encoding='utf-8'), encoding='utf-8')
        self.edit('app/extension/dashboard.html', '<span id="appVersion"></span>',
                  '<span id="appVersion">version 0.0.1</span>')
        self.assertIn('dashboard.html: the version 0.0.1 is written into the page', self.gate())

    def test_blocks_stale_instructions(self):
        self.edit('app/README.md', 'Uninstall', 'Uninstall (run Install Tools.ps1)')
        self.assertIn('README.md: contains stale text "Install Tools.ps1"', self.gate())

    def test_blocks_non_ascii_powershell(self):
        path = self.src / 'Setup.ps1'
        path.write_text(path.read_text(encoding='utf-8') + '\n# café\n', encoding='utf-8')
        self.assertIn('Setup.ps1: must be ASCII only', self.gate())

    def test_blocks_a_python_file_that_does_not_parse(self):
        path = self.src / 'app/helper/paths.py'
        path.write_text(path.read_text(encoding='utf-8') + '\ndef broken(:\n', encoding='utf-8')
        self.assertIn('helper/paths.py: is not valid text', self.gate())

    def test_blocks_unverifiable_and_insecure_tool_sources(self):
        lock = tree_lock()
        for entry in lock['components']:
            entry.pop('sha256', None)
            entry.pop('pinned', None)
        self.write_json('tools.lock.json', lock)
        self.assertIn('python: no pinned hash, publisher checksum or signature check', self.gate())
        for entry in lock['components']:
            entry['checksum'] = {'url': 'http://example.com/sums'}
        self.write_json('tools.lock.json', lock)
        self.assertIn('python: checksum url must be https', self.gate())
        for entry in lock['components']:
            entry['checksum']['url'] = 'https://example.com/sums'
        self.write_json('tools.lock.json', lock)
        self.assertIn('python: --require-pinned needs a sha256; run scripts/refresh_tool_lock.py first',
                      self.gate('--require-pinned'))

    def test_javascript_syntax_errors_block_the_release(self):
        if not shutil.which('node'):
            self.skipTest('node not available')
        self.edit('app/extension/background.js', "'use strict';", "'use strict'; function (")
        self.assertIn('JavaScript syntax error', self.gate())

    def test_blocks_dangling_html_script_reference(self):
        # A typo'd <script src> would load a blank popup; the extension static gate catches it.
        self.edit('app/extension/dashboard.html', 'src="dashboard.js"', 'src="dashboard-typo.js"')
        self.assertIn('Extension static check failed', self.gate())

    def test_blocks_a_missing_youtube_host_permission(self):
        manifest = json.loads((self.src / 'app/extension/manifest.json').read_text())
        manifest['host_permissions'].remove('https://youtu.be/*')
        self.write_json('app/extension/manifest.json', manifest)
        self.assertIn('Extension static check failed', self.gate())

    def test_invalid_manifest_json_is_a_gate_not_a_crash(self):
        (self.src / 'app/extension/manifest.json').write_text('{', encoding='utf-8')
        self.assertIn('app/extension/manifest.json: is not valid JSON', self.gate())


# --------------------------------------------------------------- source contracts ----

class ExtensionArchiveTests(unittest.TestCase):
    """The read-back of the extension archive: what it names about an archive it must refuse."""

    def setUp(self):
        self.tmp = tmpdir()
        self.src = copy_source(self.tmp / 'src')
        self.entries = br.extension_archive(br.collect(self.src))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def archive(self, name, entries):
        path = self.tmp / name
        br.build_zip(entries, path)
        return path.read_bytes()

    def test_holds_the_extension_and_nothing_else(self):
        folder = self.src / 'app' / 'extension'
        files = sorted((p for p in folder.rglob('*') if p.is_file()), key=lambda p: p.relative_to(folder).as_posix())
        self.assertEqual([name for name, _ in self.entries], [p.relative_to(folder).as_posix() for p in files])
        self.assertEqual([path for _, path in self.entries], files)
        self.assertTrue(all(not name.startswith(br.EXTENSION_PREFIX) for name, _ in self.entries))
        good = self.archive('good.zip', self.entries)
        self.assertEqual(br.extension_problems(good, self.entries, VERSION), [])
        self.assertEqual(self.archive('again.zip', self.entries), good, 'the same entries give the same bytes')
        with zipfile.ZipFile(io.BytesIO(good)) as bundle:       # one damaged byte inside the first file's data
            first = bundle.namelist()[0]
            start = bundle.getinfo(first).header_offset + 30 + len(first)
        damaged = bytearray(good)
        damaged[start + 5] ^= 0xFF
        problems = br.extension_problems(bytes(damaged), self.entries, VERSION)
        self.assertTrue(problems and all(first in problem for problem in problems), problems)

    def test_names_what_is_wrong_with_a_broken_archive(self):
        without = self.archive('without.zip', self.entries[1:])
        problems = br.extension_problems(without, self.entries, VERSION)
        self.assertEqual(len(problems), 1, problems)
        self.assertTrue(problems[0].startswith('the archive holds ['), problems)
        changed = self.tmp / 'changed.js'
        changed.write_bytes(b'something else')
        name, path = self.entries[0]
        problems = br.extension_problems(self.archive('changed.zip', [(name, changed), *self.entries[1:]]),
                                         self.entries, VERSION)
        self.assertEqual(problems, [f'{name}: the stored bytes differ from the tree'])
        good = self.archive('good.zip', self.entries)
        self.assertEqual(br.extension_problems(good, self.entries, '0.0.1'), ['manifest.json says version ' + VERSION])
        self.assertEqual(len(br.extension_problems(b'not an archive', self.entries, VERSION)), 1)
        self.assertTrue(br.extension_problems(b'not an archive', self.entries, VERSION)[0].startswith('File is not a zip'))


class SourceContractTests(unittest.TestCase):
    def test_powershell_scripts_are_ascii_so_windows_powershell_5_1_reads_them_correctly(self):
        for rel in br.SCRIPTS:
            self.assertTrue((ROOT / rel).read_bytes().isascii(), rel)

    def test_launcher_flushes_every_forwarded_message(self):
        source = (ROOT / 'app' / 'bridge' / 'NativeLauncher.cs').read_text()
        # Stream.CopyTo left small requests in a 4 KB pipe buffer, so the helper never saw them.
        self.assertNotIn('browserIn.CopyTo', source)
        self.assertNotIn('StandardOutput.BaseStream.CopyTo', source)
        self.assertGreaterEqual(source.count('.Flush();'), 2)
        self.assertIn('toChild.Write(buffer, 0, count);\n                        toChild.Flush();', source)
        self.assertIn('browserOut.Write(reply, 0, got);\n                    browserOut.Flush();', source)

    def test_setup_asks_for_the_end_to_end_helper_check(self):
        self.assertIn("'--check-host'", (ROOT / 'Setup.ps1').read_text())

    def test_manifest_stays_least_privilege(self):
        manifest = json.loads((ROOT / 'app/extension/manifest.json').read_text())
        hosts = manifest.get('host_permissions', [])
        self.assertTrue(hosts and all('youtube.com' in host or 'youtu.be' in host for host in hosts), hosts)
        self.assertNotIn('<all_urls>', json.dumps(manifest))
        self.assertEqual(manifest['manifest_version'], 3)

    def test_extension_static_check_passes_on_the_clean_tree(self):
        self.assertEqual(br._check_extension(ROOT / 'app/extension'), [])
        self.assertEqual(br._check_html_refs(ROOT / 'app/extension'), [])
        self.assertEqual(br._check_version_literals(ROOT / 'app/extension'), [])


# ---------------------------------------------------------------- the lock refresh ----

class LockRefreshTests(unittest.TestCase):
    """refresh_tool_lock against a fake web: GitHub's redirects, checksum files and downloads."""

    def setUp(self):
        self.tmp = tmpdir()
        self.blob = {'yt-dlp.exe': b'YT' * 5000, 'deno-x86_64-pc-windows-msvc.zip': b'DENO' * 5000,
                     'ffmpeg-9.0-essentials_build.zip': b'FF' * 5000, 'python.zip': b'PY' * 5000}
        yt, deno, ff, py = (self.blob[name] for name in ('yt-dlp.exe', 'deno-x86_64-pc-windows-msvc.zip',
                                                        'ffmpeg-9.0-essentials_build.zip', 'python.zip'))
        self.urls = {
            'yt-dlp': 'https://github.com/yt-dlp/yt-dlp/releases/download/2099.01.01/yt-dlp.exe',
            'deno': 'https://github.com/denoland/deno/releases/download/v9.9.9/deno-x86_64-pc-windows-msvc.zip',
            'ffmpeg': 'https://github.com/GyanD/codexffmpeg/releases/download/9.0/ffmpeg-9.0-essentials_build.zip',
            'python': 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip'}
        self.sums = {'yt-dlp': f'{sha(yt)}  yt-dlp.exe', 'deno': f'{sha(deno)}  deno-x86_64-pc-windows-msvc.zip',
                     'ffmpeg': f'{sha(ff)}  ffmpeg-9.0-essentials_build.zip'}
        bundle = b'{"verificationMaterial": {"tlogEntries": [{"logIndex": 7}]}}'
        self.http = FakeHttp(
            {self.urls['python']: py, self.urls['python'] + '.sigstore': bundle,
             'https://www.gyan.dev/ffmpeg/builds/release-version': b'9.0\n',
             'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip.sha256': self.sums['ffmpeg'].encode(),
             'https://github.com/yt-dlp/yt-dlp/releases/download/2099.01.01/SHA2-256SUMS': (self.sums['yt-dlp'] + '\n'
                                                                                          f'{sha(deno)}  other.exe\n').encode(),
             'https://github.com/denoland/deno/releases/download/v9.9.9/deno-x86_64-pc-windows-msvc.zip.sha256sum':
                 (self.sums['deno'] + '\n').encode(),
             **{url: self.blob[name] for name, url in (('yt-dlp.exe', self.urls['yt-dlp']),
                                                       ('deno-x86_64-pc-windows-msvc.zip', self.urls['deno']),
                                                       ('ffmpeg-9.0-essentials_build.zip', self.urls['ffmpeg']))}},
            {'https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe': self.urls['yt-dlp'],
             'https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip': self.urls['deno']})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def refresh(self, lock, **options):
        with mock.patch.object(rl.sv, 'verify', return_value={
                'identity': 'thomas@python.org', 'issuer': 'https://accounts.google.com', 'log_index': 7}) as verify:
            refreshed = rl.refresh(lock, opener=self.http, redirect=self.http, cache=self.tmp / 'cache', **options)
        return refreshed, verify

    def test_pins_exact_versions_and_cross_checks_publisher_checksums(self):
        lock = json.loads((Path(ROOT) / 'tools.lock.json').read_text())
        refreshed, verify = self.refresh(lock)
        by = {component['name']: component for component in refreshed['components']}
        self.assertEqual((by['yt-dlp']['version'], by['yt-dlp']['sha256'], by['yt-dlp']['url']),
                         ('2099.01.01', sha(self.blob['yt-dlp.exe']), self.urls['yt-dlp']))
        self.assertEqual((by['deno']['version'], by['deno']['sha256'], by['deno']['url']),
                         ('9.9.9', sha(self.blob['deno-x86_64-pc-windows-msvc.zip']), self.urls['deno']))
        self.assertEqual((by['ffmpeg']['version'], by['ffmpeg']['url']), ('9.0', self.urls['ffmpeg']))
        self.assertEqual(by['python']['sha256'], sha(self.blob['python.zip']))
        self.assertTrue(all('checksum' not in component and 'authenticode' not in component
                            for component in by.values()))
        evidence = refreshed['pinned']['evidence']
        self.assertEqual(evidence['yt-dlp'], 'publisher checksum SHA2-256SUMS')
        self.assertIn('gyan.dev', evidence['ffmpeg'])
        self.assertIn('Sigstore', evidence['python'])
        verify.assert_called_once()

    def test_refuses_to_pin_a_download_without_a_publisher_checksum(self):
        del self.http.routes['https://github.com/yt-dlp/yt-dlp/releases/download/2099.01.01/SHA2-256SUMS']
        with self.assertRaisesRegex(rl.LockError, 'yt-dlp: .* no publisher checksum'):
            self.refresh({'components': [{'name': 'yt-dlp', 'version': 'latest'}]})
        self.assertFalse((self.tmp / 'cache' / f"yt-dlp-{sha(self.blob['yt-dlp.exe'])[:16]}.bin").exists())

    def test_refuses_a_download_that_differs_from_the_publisher_checksum(self):
        self.http.routes[self.urls['yt-dlp']] = b'tampered' * 5000
        with self.assertRaisesRegex(rl.LockError, 'yt-dlp: .*Checksum mismatch'):
            self.refresh({'components': [{'name': 'yt-dlp', 'version': 'latest'}]})

    def test_ffmpeg_checksum_falls_back_to_the_github_release_file(self):
        del self.http.routes['https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip.sha256']
        self.http.routes[self.urls['ffmpeg'] + '.sha256'] = (self.sums['ffmpeg'] + '\n').encode()
        refreshed, _ = self.refresh({'components': [{'name': 'ffmpeg', 'version': 'x'}]})
        self.assertIn('codexffmpeg', refreshed['pinned']['evidence']['ffmpeg'])

    def test_python_is_pinned_only_after_sigstore_confirmed_the_signer(self):
        lock = {'components': [{'name': 'python', 'version': '3.12.10', 'url': self.urls['python']}]}
        refreshed, verify = self.refresh(lock)
        self.assertEqual(refreshed['components'][0]['sha256'], sha(self.blob['python.zip']))
        self.assertEqual(refreshed['components'][0]['version'], '3.12.10')      # never bumped by the refresh
        args, _ = verify.call_args
        self.assertEqual((args[0], args[1].name, args[2].name), ('3.12.10', 'python-3.12.10-embed-amd64.zip',
                                                                 'python-3.12.10-embed-amd64.zip.sigstore'))
        with mock.patch.object(rl.sv, 'verify', side_effect=sv.VerifyError('Signature verification failed')):
            with self.assertRaisesRegex(rl.LockError, 'python: the download was not confirmed.*Signature verification '
                                                      'failed'):
                rl.refresh(lock, opener=self.http, redirect=self.http, cache=self.tmp / 'cache2')
        self.assertFalse(list((self.tmp / 'cache2').glob('python-*.bin')))

    def test_python_without_a_sigstore_bundle_is_not_pinned(self):
        del self.http.routes[self.urls['python'] + '.sigstore']
        with self.assertRaisesRegex(rl.LockError, 'no Sigstore bundle'):
            self.refresh({'components': [{'name': 'python', 'version': '3.12.10', 'url': self.urls['python']}]})

    def test_latest_tag_is_read_from_the_redirect(self):
        self.assertEqual(rl.latest_tag('yt-dlp/yt-dlp', 'yt-dlp.exe', self.http), '2099.01.01')
        self.assertEqual(rl.latest_tag('denoland/deno', 'deno-x86_64-pc-windows-msvc.zip', self.http), 'v9.9.9')
        self.assertEqual(self.http.hits[-2:], ['https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe',
                                               'https://github.com/denoland/deno/releases/latest/download/'
                                               'deno-x86_64-pc-windows-msvc.zip'])   # HEAD, nothing downloaded
        bad = FakeHttp({}, {'https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe':
                            'https://example.com/elsewhere/yt-dlp.exe'})
        with self.assertRaisesRegex(rl.LockError, 'not a release download of yt-dlp.exe'):
            rl.latest_tag('yt-dlp/yt-dlp', 'yt-dlp.exe', bad)
        with self.assertRaisesRegex(rl.LockError, 'HTTP 404'):
            rl.latest_tag('yt-dlp/yt-dlp', 'no-such-asset.exe', self.http)

    def test_check_reports_everything_that_stops_a_pinned_lock_from_working(self):
        entry = {'name': 'yt-dlp', 'version': '2099.01.01', 'url': self.urls['yt-dlp'], 'sha256': sha(self.blob['yt-dlp.exe'])}
        with contextlib.redirect_stdout(io.StringIO()):           # the intact entry prints 'yt-dlp: ok'
            self.assertEqual(rl.check({'components': [entry]}, self.http), [])
            stale = dict(entry, sha256=sha(b'other'))
            self.assertEqual(rl.check({'components': [dict(entry, sha256=None), stale]}, self.http), [
                'yt-dlp: not pinned',
                f"yt-dlp: {self.urls['yt-dlp']} now serves a file with SHA-256 {entry['sha256']}, "
                f"the lock pins {stale['sha256']}"])
            self.http.routes.pop(self.urls['yt-dlp'])
            problems = rl.check({'components': [entry]}, self.http)
        self.assertEqual(len(problems), 1)
        self.assertIn('yt-dlp: Download failed', problems[0])

    def test_signer_comes_from_python_orgs_table(self):
        self.assertEqual(sv.signer('3.12.10'), ('thomas@python.org', 'https://accounts.google.com'))
        self.assertEqual(sv.signer('3.12'), sv.signer('3.12.9'))
        with self.assertRaisesRegex(sv.VerifyError, 'downloads/metadata/sigstore'):
            sv.signer('4.0')

    def test_verify_runs_the_client_with_the_published_identity(self):
        bundle = self.tmp / 'x.sigstore'
        bundle.write_text('{"verificationMaterial": {"tlogEntries": [{"logIndex": 42, "logId": "abc"}]}}')
        for code, outcome in ((0, None), (1, sv.VerifyError), (2, sv.VerifyError)):
            run = mock.Mock(returncode=code, stdout='OK: x', stderr='')
            with self.subTest(exit=code), mock.patch.object(sv.subprocess, 'run', return_value=run) as run_mock:
                if outcome is None:
                    report = sv.verify('3.12.10', self.tmp / 'x.zip', bundle)
                    self.assertEqual((report['identity'], report['log_index'], report['log_id']),
                                     ('thomas@python.org', 42, 'abc'))
                else:
                    with self.assertRaises(outcome):
                        sv.verify('3.12.10', self.tmp / 'x.zip', bundle)
                command = run_mock.call_args[0][0]
                self.assertEqual(command[command.index('--cert-identity') + 1], 'thomas@python.org')
                self.assertEqual(command[command.index('--cert-oidc-issuer') + 1], 'https://accounts.google.com')
                self.assertEqual(command[-1], str(self.tmp / 'x.zip'))


if __name__ == '__main__':
    unittest.main()
