import contextlib
import io
import json
import os
import shutil
import ssl
import stat
import subprocess
import sys
import textwrap
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

from support import ROOT, tmpdir
from installer_support import (EXT_ID, Server, fake_components, make_payload, sha, tree_snapshot, zip_bytes)

import bmdfs
import installer_core as ic


class FsSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.box = self.tmp / 'box'
        self.box.mkdir()
        self.outside = self.tmp / 'outside'
        self.outside.mkdir()
        (self.outside / 'precious.txt').write_text('keep')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rmtree_stays_inside_and_never_follows_links(self):
        inner = self.box / 'a' / 'b'
        inner.mkdir(parents=True)
        (inner / 'f.txt').write_text('x')
        os.symlink(self.outside, self.box / 'a' / 'escape')
        os.symlink(self.outside / 'precious.txt', self.box / 'a' / 'filelink')
        bmdfs.safe_rmtree(self.box / 'a', self.box)
        self.assertFalse((self.box / 'a').exists())
        self.assertEqual((self.outside / 'precious.txt').read_text(), 'keep')

    def test_refuses_outside_boundary_itself_and_traversal(self):
        with self.assertRaises(bmdfs.BoundaryError):
            bmdfs.safe_rmtree(self.outside, self.box)
        with self.assertRaises(bmdfs.BoundaryError):
            bmdfs.safe_rmtree(self.box, self.box)
        with self.assertRaises(bmdfs.BoundaryError):
            bmdfs.safe_rmtree(self.box / '..' / 'outside', self.box)
        with self.assertRaises(bmdfs.BoundaryError):
            bmdfs.safe_unlink(self.outside / 'precious.txt', self.box)
        os.symlink(self.outside, self.box / 'jump')
        with self.assertRaises(bmdfs.BoundaryError):
            bmdfs.safe_unlink(self.box / 'jump' / 'precious.txt', self.box)
        self.assertTrue((self.outside / 'precious.txt').exists())

    def test_boundaries_hold_with_spaces_non_ascii_trailing_dots_and_symlink_loops(self):
        # NTFS refuses a component with a trailing space, so that part is POSIX-only;
        # the non-ASCII text, the inner spaces and the inner dots stay in both names.
        stem = 'Álbum  ñ 日本語 (x)' + ('' if os.name == 'nt' else ' ')
        weird = self.box / stem / 'sub . folder'
        weird.mkdir(parents=True)
        (weird / 'f ü.txt').write_text('x')
        os.symlink(self.box / stem, weird / 'loop')          # a link back up the tree
        os.symlink(self.outside, weird / 'to outside')
        bmdfs.safe_rmtree(self.box / stem, self.box)
        self.assertFalse((self.box / stem).exists())
        self.assertEqual((self.outside / 'precious.txt').read_text(), 'keep')
        inside = self.box / 'dir with space'
        inside.mkdir()
        with self.assertRaises(bmdfs.BoundaryError):
            bmdfs.safe_rmtree(inside / '..' / '..' / 'outside', self.box)
        (self.tmp / 'box sibling').mkdir()                                     # shares the prefix of the boundary
        with self.assertRaises(bmdfs.BoundaryError):
            bmdfs.safe_rmtree(str(self.box) + ' sibling', self.box)            # a prefix match is not containment
        with self.assertRaises(bmdfs.BoundaryError):
            bmdfs.safe_rmtree(self.tmp / 'box sibling', self.box)
        self.assertTrue((self.tmp / 'box sibling').exists())

    def test_top_level_symlink_is_unlinked_not_followed(self):
        os.symlink(self.outside, self.box / 'link')
        bmdfs.safe_rmtree(self.box / 'link', self.box)
        self.assertFalse(os.path.lexists(self.box / 'link'))
        self.assertTrue((self.outside / 'precious.txt').exists())

    def test_commit_new_file_never_overwrites(self):
        src, dst = self.box / 's.tmp', self.box / 'd.wav'
        src.write_bytes(b'new'); dst.write_bytes(b'user')
        with self.assertRaises(FileExistsError):
            bmdfs.commit_new_file(src, dst)
        self.assertEqual(dst.read_bytes(), b'user')
        dst.unlink()
        bmdfs.commit_new_file(src, dst)
        self.assertEqual((dst.read_bytes(), src.exists()), (b'new', False))

    def test_commit_fallback_when_hard_links_are_unsupported(self):
        src, dst = self.box / 's.tmp', self.box / 'd.wav'
        src.write_bytes(b'new')
        with mock.patch('os.link', side_effect=OSError('no hard links on exFAT')):
            bmdfs.commit_new_file(src, dst)
            self.assertEqual(dst.read_bytes(), b'new')
            src.write_bytes(b'other')
            with self.assertRaises(FileExistsError):
                bmdfs.commit_new_file(src, dst)
        self.assertEqual(dst.read_bytes(), b'new')

    def test_atomic_json_never_leaves_torn_files(self):
        target = self.box / 'm.json'
        bmdfs.write_json_atomic(target, {'a': 1})
        with mock.patch('json.dump', side_effect=RuntimeError('disk full')):
            with self.assertRaises(RuntimeError):
                bmdfs.write_json_atomic(target, {'a': 2})
        self.assertEqual(json.loads(target.read_text()), {'a': 1})
        self.assertEqual([p.name for p in self.box.iterdir()], ['m.json'])


class RegistryKeyTests(unittest.TestCase):
    def test_native_host_keys_are_exactly_what_the_browsers_read(self):
        self.assertEqual(ic.BROWSER_KEYS, {
            'Chrome': 'Software\\Google\\Chrome\\NativeMessagingHosts\\com.bravemusic.downloader',
            'Brave': 'Software\\BraveSoftware\\Brave-Browser\\NativeMessagingHosts\\com.bravemusic.downloader'})
        for key in ic.BROWSER_KEYS.values():
            self.assertNotIn('\\\\', key)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.server = Server()

    def tearDown(self):
        self.server.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_verified_download_and_cache_reuse(self):
        data = os.urandom(300000)
        self.server.routes['/f'] = data
        dest = self.tmp / 'd' / 'f.bin'
        ic.download_verified(self.server.base + '/f', dest, sha(data))
        self.assertEqual(dest.read_bytes(), data)
        hits = len(self.server.hits)
        ic.download_verified(self.server.base + '/f', dest, sha(data))
        self.assertEqual(len(self.server.hits), hits, 'verified file is reused, not downloaded again')

    def test_bad_checksum_discards_file(self):
        self.server.routes['/f'] = b'tampered' * 5000
        dest = self.tmp / 'f.bin'
        with self.assertRaisesRegex(ic.InstallError, 'Checksum mismatch'):
            ic.download_verified(self.server.base + '/f', dest, sha(b'genuine'))
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_interrupted_download_resumes_with_range(self):
        data = os.urandom(900000)
        self.server.routes['/f'] = data
        self.server.drop['/f'] = 300000
        dest = self.tmp / 'f.bin'
        ic.download_verified(self.server.base + '/f', dest, sha(data), delay=0.01)
        self.assertEqual(sha(dest.read_bytes()), sha(data))
        self.assertEqual(self.server.hits.count('/f'), 2)

    def test_refuses_plain_http_to_the_internet_and_404(self):
        with self.assertRaisesRegex(ic.InstallError, 'non-HTTPS'):
            ic.download_verified('http://example.com/x.exe', self.tmp / 'x', 'a' * 64)
        with self.assertRaisesRegex(ic.InstallError, 'Download failed'):
            ic.download_verified(self.server.base + '/missing', self.tmp / 'm', 'a' * 64)

    def test_checksum_file_parsing(self):
        h = 'ab' * 32
        other = 'cd' * 32
        name = 'deno-x86_64-pc-windows-msvc.zip'
        self.assertEqual(ic.parse_checksum_text(f'{h}  yt-dlp.exe\n{other}  o.exe', 'yt-dlp.exe'), h)
        self.assertEqual(ic.parse_checksum_text(h + '\n'), h)
        self.assertEqual(ic.parse_checksum_text(f'{h} *deno.zip', 'deno.zip'), h)
        layouts = {
            'sha256sum': f'{h}  {name}\n',
            'bsd': f'SHA256 ({name}) = {h}\n',
            'powershell list': (f'\r\nAlgorithm : SHA256\r\nHash      : {h.upper()}\r\n'
                                f'Path      : D:\\a\\deno\\deno\\target\\release\\{name}\r\n\r\n'),
            'powershell table': ('Algorithm       Hash                                                             Path\r\n'
                                 '---------       ----                                                             ----\r\n'
                                 f'SHA256          {h.upper()}   C:\\build\\{name}\r\n'),
            'bare with newline and spaces': f'  {h}  \r\n',
        }
        for label, text in layouts.items():
            self.assertEqual(ic.parse_checksum_text(text, name), h, label)
        multi = f'{other}  other.zip\n{h}  {name}\n'
        self.assertEqual(ic.parse_checksum_text(multi, name), h)
        for bad in ('not a hash', '', f'{h}  a\n{other}  b', f'{h[:-1]}\n', f'{h}0\n'):
            with self.assertRaises(ic.InstallError):
                ic.parse_checksum_text(bad, 'zzz')
        with self.assertRaisesRegex(ic.InstallError, 'file started with'):
            ic.parse_checksum_text('<html>Not Found</html>', name)

    def test_unverifiable_component_is_refused_unless_opted_in(self):
        entry = {'name': 'x', 'url': self.server.base + '/f'}
        with self.assertRaisesRegex(ic.InstallError, 'refusing'):
            ic.resolve_expected(entry)
        self.assertEqual(ic.resolve_expected(entry, allow_unverified=True), (None, 'unverified'))
        self.assertEqual(ic.resolve_expected({'name': 'p', 'authenticode': 'PSF'}), (None, 'authenticode'))

    def test_zip_slip_and_links_are_rejected(self):
        evil = self.tmp / 'evil.zip'
        with zipfile.ZipFile(evil, 'w') as z:
            z.writestr('../../escape.txt', 'x')
        with self.assertRaisesRegex(ic.InstallError, 'Unsafe'):
            ic.safe_extract(evil, self.tmp / 'out')
        link = self.tmp / 'link.zip'
        with zipfile.ZipFile(link, 'w') as z:
            info = zipfile.ZipInfo('ln')
            info.external_attr = (0o120777 << 16)
            z.writestr(info, '/etc/passwd')
        with self.assertRaisesRegex(ic.InstallError, 'link'):
            ic.safe_extract(link, self.tmp / 'out2')
        self.assertFalse((self.tmp.parent / 'escape.txt').exists())


class NetworkFallbackTests(unittest.TestCase):
    """Real-world bug: the bundled Python sometimes cannot validate a site's certificate chain."""

    def setUp(self):
        self.tmp = tmpdir()
        self.request = urllib.request.Request('https://github.com/x/releases/latest/download/SHA2-256SUMS')
        self.tls = urllib.error.URLError(ssl.SSLCertVerificationError(1, 'unable to get issuer certificate'))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_certificate_trust_errors_fall_back_to_windows_but_nothing_else_does(self):
        sentinel = object()
        with mock.patch('urllib.request.urlopen', side_effect=self.tls), \
                mock.patch.object(ic, 'powershell_open', return_value=sentinel) as fallback:
            with mock.patch.object(ic, 'on_windows', return_value=True):
                self.assertIs(ic.default_opener(self.request), sentinel)
                fallback.assert_called_once_with(self.request)
            with mock.patch.object(ic, 'on_windows', return_value=False):
                with self.assertRaises(urllib.error.URLError):
                    ic.default_opener(self.request)
            fallback.reset_mock()
            for other in (urllib.error.URLError(OSError('connection refused')),
                          urllib.error.HTTPError(self.request.full_url, 404, 'Not Found', {}, None)):
                with mock.patch('urllib.request.urlopen', side_effect=other), \
                        mock.patch.object(ic, 'on_windows', return_value=True):
                    with self.assertRaises(urllib.error.URLError):
                        ic.default_opener(self.request)
            fallback.assert_not_called()

    def test_windows_fetch_passes_the_url_through_the_environment_and_cleans_up(self):
        seen = {}

        def fake_powershell(command, **kwargs):
            seen['command'], seen['env'] = command, kwargs['env']
            Path(kwargs['env']['BMD_FETCH_OUT']).write_bytes(b'hello' * 1000)
            return subprocess.CompletedProcess(command, 0, '', '')

        with mock.patch('subprocess.run', fake_powershell):
            response = ic.powershell_open(self.request)
        self.assertNotIn(self.request.full_url, ' '.join(seen['command']))
        self.assertEqual(seen['env']['BMD_FETCH_URL'], self.request.full_url)
        folder = Path(seen['env']['BMD_FETCH_OUT']).parent
        with response:
            self.assertEqual((response.status, response.headers['Content-Length']), (200, '5000'))
            self.assertEqual(response.read(5), b'hello')
        self.assertFalse(folder.exists(), 'temporary download is removed')
        failing = subprocess.CompletedProcess([], 1, '', 'The remote name could not be resolved')
        with mock.patch('subprocess.run', return_value=failing):
            with self.assertRaisesRegex(urllib.error.URLError, 'Windows could not fetch'):
                ic.powershell_open(self.request)
        with self.assertRaises(ic.InstallError):
            ic.powershell_open(urllib.request.Request('http://evil.example/x'))

    def test_a_download_through_the_windows_fallback_still_has_to_match_its_hash(self):
        data = os.urandom(200000)
        source = self.tmp / 'windows-copy.bin'
        source.write_bytes(data)

        def opener(request, timeout=0):
            folder = self.tmp / ('r' + str(len(list(self.tmp.iterdir()))))
            folder.mkdir()
            shutil.copy(source, folder / 'download.bin')
            return ic._FileResponse(folder / 'download.bin', folder)

        dest = self.tmp / 'out' / 'f.bin'
        (dest.parent).mkdir()
        (dest.parent / 'f.bin.partial').write_bytes(data[:1000])         # a stale partial from an earlier try
        ic.download_verified('https://example.com/f', dest, sha(data), opener=opener)
        self.assertEqual(dest.read_bytes(), data)
        dest.unlink()
        with self.assertRaisesRegex(ic.InstallError, 'Checksum mismatch'):
            ic.download_verified('https://example.com/f', dest, sha(b'something else'), opener=opener)
        self.assertFalse(dest.exists())

    def test_checksum_fetch_retries_transient_failures_but_not_http_errors(self):
        calls = []

        class Page(io.BytesIO):
            pass

        def flaky(request, timeout=0):
            calls.append(1)
            if len(calls) < 3:
                raise self.tls
            return Page(b'abc')

        self.assertEqual(ic.fetch_text('https://example.com/s', flaky, attempts=3, delay=0), 'abc')
        self.assertEqual(len(calls), 3)
        calls.clear()

        def not_found(request, timeout=0):
            calls.append(1)
            raise urllib.error.HTTPError(request.full_url, 404, 'Not Found', {}, None)

        with self.assertRaisesRegex(ic.InstallError, 'Could not download'):
            ic.fetch_text('https://example.com/s', not_found, attempts=3, delay=0)
        self.assertEqual(len(calls), 1)
        with self.assertRaisesRegex(ic.InstallError, 'Could not download'):
            ic.fetch_text('https://example.com/s', lambda r, timeout=0: (_ for _ in ()).throw(self.tls),
                          attempts=2, delay=0)


class InstallBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.server = Server()
        self.lock = fake_components(self.server)
        self.layout = ic.Layout(self.tmp / 'Local' / 'BMD', self.tmp / 'Roaming' / 'BMD', self.tmp / 'Music')
        self.registry = ic.FakeRegistry()
        self.payload = make_payload(self.tmp)
        (self.layout.output).mkdir(parents=True)
        (self.layout.output / 'song.wav').write_bytes(b'MUSIC')
        self.layout.data_dir.mkdir(parents=True)
        (self.layout.data_dir / 'queue.sqlite3').write_bytes(b'HISTORY')

    def tearDown(self):
        self.server.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def install(self, **kw):
        kw.setdefault('rename_retries', 1)
        return ic.install(self.layout, self.payload, self.lock, self.registry, extension_id=EXT_ID, **kw)

    def make_old_app(self, version_note='old'):
        app = self.layout.app
        for rel, data in {'extension/manifest.json': (self.payload and zipfile.ZipFile(self.payload).read('extension/manifest.json').decode()),
                          'bridge/host-paths.txt': 'old', 'helper/native_host.py': version_note,
                          'tools/yt-dlp.exe': 'OLDYT', 'python/python.exe': 'OLDPY'}.items():
            (app / rel).parent.mkdir(parents=True, exist_ok=True)
            (app / rel).write_text(data)

    def snapshot(self):
        # Verified downloads are deliberately kept after a failure so a retry does not download again.
        return {k: v for k, v in tree_snapshot(self.layout.root).items() if not k.startswith('cache')}

    def leftovers(self):
        return [p.name for p in self.layout.root.iterdir()
                if p.name.startswith(('app.', 'install-journal', '.install.lock')) and p.name != 'app']


def script(folder, name, body):
    path = Path(folder) / name
    path.write_text(textwrap.dedent(body))
    return [sys.executable, str(path)]


REPLY_PRELUDE = """
import json, struct, sys
def read_request():
    header = sys.stdin.buffer.read(4)
    return json.loads(sys.stdin.buffer.read(struct.unpack('<I', header)[0]))
def reply(obj):
    raw = json.dumps(obj).encode()
    sys.stdout.buffer.write(struct.pack('<I', len(raw)) + raw)
    sys.stdout.buffer.flush()
"""


class HostRoundTripTests(unittest.TestCase):
    """The browser keeps stdin open after sending a request; the helper must still answer."""

    def setUp(self):
        self.tmp = tmpdir()
        self.env = dict(os.environ, BMD_INSTALL_ROOT=str(self.tmp / 'root'), BMD_DATA_DIR=str(self.tmp / 'data'),
                        BMD_OUTPUT=str(self.tmp / 'music'), PYTHONDONTWRITEBYTECODE='1')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_real_native_host_answers_a_health_request(self):
        host = ROOT / 'app' / 'helper' / 'native_host.py'
        data = ic.native_roundtrip([sys.executable, str(host)], env=self.env)
        self.assertEqual(data['version'], '2.5.0')
        self.assertTrue((self.tmp / 'data' / 'queue.sqlite3').exists())

    def test_a_launcher_that_only_forwards_input_when_stdin_closes_is_detected(self):
        # Reproduces the real bug: requests sat in a 4 KB buffer until the browser closed stdin.
        command = script(self.tmp, 'buffered.py', REPLY_PRELUDE + """
sys.stdin.buffer.read()            # waits for EOF before doing anything
reply({'id': 1, 'ok': True, 'data': {'version': '2.5.0'}})
""")
        with self.assertRaisesRegex(ic.InstallError, 'stayed open'):
            ic.native_roundtrip(command, env=self.env, timeout=1)

    def test_silent_crashing_noisy_and_wrong_version_hosts_are_all_rejected_with_reasons(self):
        cases = {
            'no reply': ('import sys, time\nsys.stdin.buffer.read(1)\ntime.sleep(30)\n', 'no reply within'),
            'crash': ('import sys\nsys.stderr.write("ModuleNotFoundError: helper")\nsys.exit(3)\n', 'ModuleNotFoundError'),
            'junk': ('import sys\nsys.stdin.buffer.read(1)\nsys.stdout.buffer.write(b"hello world, not a frame")\nsys.stdout.buffer.flush()\n', 'bad frame size'),
            'wrong version': (REPLY_PRELUDE + "read_request()\nreply({'id': 1, 'ok': True, 'data': {'version': '1.0'}})\n", 'reports version 1.0'),
            'error reply': (REPLY_PRELUDE + "read_request()\nreply({'id': 1, 'ok': False, 'error': 'nope'})\n", 'replied with an error'),
        }
        for label, (body, expected) in cases.items():
            with self.subTest(label):
                command = script(self.tmp, label.replace(' ', '_') + '.py', body)
                with self.assertRaisesRegex(ic.InstallError, expected):
                    ic.native_roundtrip(command, env=self.env, timeout=2)

    def test_a_missing_launcher_is_an_error_not_a_crash(self):
        with self.assertRaises(OSError):
            ic.native_roundtrip([str(self.tmp / 'nope.exe')], env=self.env)


class HostCheckInstallTests(InstallBase):
    def test_install_runs_the_helper_check_with_the_new_files_in_place(self):
        seen = []

        def check(layout):
            seen.append((layout.app / 'bridge' / 'host-paths.txt').is_file())

        self.install(host_check=check)
        self.assertEqual(seen, [True])

    def test_a_helper_that_cannot_answer_rolls_the_whole_install_back(self):
        self.make_old_app()
        ic.bmdfs.write_json_atomic(self.layout.manifest, {'product': 'BraveMusicDownloader', 'version': 'old'})
        before = self.snapshot()
        prev = r'C:\old\manifest.json'
        self.registry.set(ic.BROWSER_KEYS['Brave'], prev)

        def silent(layout):
            raise ic.InstallError('The helper did not answer (no reply within 20 s).')

        with self.assertRaisesRegex(ic.InstallError, 'did not answer'):
            self.install(host_check=silent)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.registry.get(ic.BROWSER_KEYS['Brave']), prev)

    def make_launcher(self):
        host = ROOT / 'app' / 'helper' / 'native_host.py'
        if os.name == 'nt':
            # Windows cannot run a shell script: build the real bridge the way setup builds it
            # (with the .NET Framework that ships with Windows) and let it start this interpreter.
            ic.build_launcher_windows(str(ROOT / 'app' / 'bridge' / 'NativeLauncher.cs'),
                                      str(self.layout.launcher))
            (self.layout.app / 'bridge' / 'host-paths.txt').write_text(f'{sys.executable}\n{host}\n',
                                                                       encoding='utf-8')
        else:
            self.layout.launcher.write_text(f'#!/bin/sh\nexec {sys.executable} {host}\n')
            self.layout.launcher.chmod(self.layout.launcher.stat().st_mode | stat.S_IEXEC)

    def test_hostcheck_explains_a_broken_setup_and_passes_a_working_one(self):
        env = {'BMD_INSTALL_ROOT': str(self.tmp / 'hostroot'), 'BMD_DATA_DIR': str(self.tmp / 'hostdata'),
               'BMD_OUTPUT': str(self.tmp / 'hostmusic'), 'PYTHONDONTWRITEBYTECODE': '1'}
        self.install()
        with mock.patch.dict(os.environ, env):
            broken = ic.hostcheck(self.layout, self.registry)
            self.assertFalse(broken['ok'])
            self.assertTrue(any(l.startswith('FAIL launcher exists') for l in broken['lines']))
            self.assertTrue(any(l.startswith('FAIL helper answers') for l in broken['lines']))
            self.make_launcher()
            good = ic.hostcheck(self.layout, self.registry)
            self.assertTrue(good['ok'], good['lines'])
            self.assertEqual(len(good['lines']), 8)
            wrong = json.loads(self.layout.native_manifest.read_text())
            wrong['allowed_origins'] = ['chrome-extension://' + 'a' * 32 + '/']
            self.layout.native_manifest.write_text(json.dumps(wrong))
            self.registry.set(ic.BROWSER_KEYS['Brave'], r'C:\somewhere\else.json')
            report = ic.hostcheck(self.layout, self.registry)
        failing = [l for l in report['lines'] if l.startswith('FAIL')]
        self.assertTrue(any('manifest allows this extension' in l and EXT_ID in l for l in failing), failing)
        self.assertTrue(any('Brave registration' in l and 'somewhere' in l for l in failing), failing)


class BusyFolderTests(InstallBase):
    """Real-world bug: a browser-restarted helper kept the app folder locked during an upgrade."""

    def test_replace_dir_stops_processes_before_each_retry_and_names_the_lock(self):
        src, dst = self.tmp / 'a', self.tmp / 'b'
        src.mkdir()
        calls = []
        real = os.rename
        denied = {'n': 0}

        def flaky(a, b):
            if Path(a) == src and denied['n'] < 2:
                denied['n'] += 1
                raise PermissionError('in use')
            return real(a, b)

        with mock.patch('os.rename', flaky):
            ic._replace_dir(src, dst, 5, delay=0, before_retry=lambda: calls.append('stop'))
        self.assertEqual((calls, dst.exists()), (['stop', 'stop'], True))
        dst.rename(src)
        with mock.patch('os.rename', side_effect=PermissionError('in use')):
            with self.assertRaises(ic.InstallError) as caught:
                ic._replace_dir(src, dst, 3, delay=0, before_retry=lambda: calls.append('stop'),
                                describe_lock=lambda: ['brave.exe', 'Windows Explorer'])
        message = str(caught.exception)
        self.assertIn('Files are in use by: brave.exe, Windows Explorer', message)
        self.assertIn('Close Brave completely', message)
        self.assertEqual(calls.count('stop'), 2 + 2)

    def test_an_upgrade_survives_a_helper_that_respawns_during_the_swap(self):
        self.make_old_app()
        stops, real, denied = [], os.rename, {'n': 0}

        def respawning(a, b):
            if Path(a) == self.layout.app and denied['n'] < 3:
                denied['n'] += 1
                raise PermissionError('a freshly started helper holds the folder')
            return real(a, b)

        with mock.patch('os.rename', respawning):
            self.install(stopper=lambda folder: stops.append(Path(folder)), rename_retries=10)
        self.assertEqual(denied['n'], 3)
        self.assertGreaterEqual(len(stops), 4)                 # once up front + before each retry
        self.assertTrue(all(s == self.layout.app for s in stops))
        self.assertTrue(self.layout.manifest.exists())
        self.assertEqual(self.leftovers(), [])

    def test_a_folder_that_stays_locked_aborts_cleanly_and_says_who_holds_it(self):
        self.make_old_app()
        before = self.snapshot()
        with mock.patch('os.rename', side_effect=PermissionError('in use')), \
                mock.patch.object(ic, 'locking_processes', return_value=['brave.exe']):
            with self.assertRaisesRegex(ic.InstallError, 'in use by: brave.exe'):
                self.install(stopper=lambda folder: None, rename_retries=3)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.registry.keys, {})

    def test_process_helpers_only_run_on_windows_and_never_splice_paths_into_the_script(self):
        with mock.patch('subprocess.run') as run:
            ic.stop_processes_under(self.tmp)
            if ic.on_windows():
                # Here the helper IS expected to run (it is the real platform); the data it
                # protects still travels in the environment, never in the command line.
                command, env = run.call_args[0][0], run.call_args[1]['env']
                self.assertNotIn(str(self.tmp), ' '.join(command))
                self.assertEqual(env['BMD_STOP_UNDER'], str(self.tmp))
            else:
                run.assert_not_called()
        self.assertEqual(ic.locking_processes(self.tmp), [])
        with mock.patch.object(ic, 'on_windows', return_value=True), mock.patch('subprocess.run') as run:
            ic.stop_processes_under(r"C:\Users\x'; Remove-Item C:\ -Recurse; '\app")
        command, env = run.call_args[0][0], run.call_args[1]['env']
        self.assertNotIn('Remove-Item', ' '.join(command))
        self.assertIn('Remove-Item', env['BMD_STOP_UNDER'])        # data travels in the environment only

    def test_setup_own_helper_check_is_allowed_while_the_installer_holds_its_lock(self):
        seen = {}
        self.layout.launcher.parent.mkdir(parents=True, exist_ok=True)

        def fake(command, env=None, **kw):
            seen['env'] = env
            return {'version': '2.5.0', 'output': 'x'}

        with mock.patch.object(ic, 'native_roundtrip', fake):
            ic.default_host_check(self.layout)
        self.assertEqual(seen['env']['BMD_ALLOW_DURING_INSTALL'], '1')


class HelperStaysOutOfSetupsWayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.root = self.tmp / 'root'
        self.root.mkdir()
        self.lock = self.root / '.install.lock'
        self.host = [sys.executable, str(ROOT / 'app' / 'helper' / 'native_host.py')]
        self.env = dict(os.environ, BMD_INSTALL_ROOT=str(self.root), BMD_DATA_DIR=str(self.tmp / 'data'),
                        BMD_OUTPUT=str(self.tmp / 'music'), PYTHONDONTWRITEBYTECODE='1')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def runs(self, env=None):
        return ic.native_roundtrip(self.host, env=env or self.env, timeout=30)['version'] == '2.5.0'

    def test_pid_alive_never_signals_and_handles_nonsense(self):
        self.assertTrue(ic.bmdfs.pid_alive(os.getpid()))
        for pid in (0, -5, 99999999):
            self.assertFalse(ic.bmdfs.pid_alive(pid))

    def test_helper_declines_while_a_live_setup_holds_the_lock(self):
        self.lock.write_text(str(os.getpid()))
        run = subprocess.run(self.host, env=self.env, input=b'', capture_output=True, timeout=30)
        self.assertEqual(run.returncode, 3)
        self.assertIn(b'setup is running', run.stderr)
        self.assertFalse((self.tmp / 'data').exists(), 'it touched nothing')

    def test_helper_starts_when_the_lock_is_stale_dead_garbage_or_explicitly_allowed(self):
        self.lock.write_text(str(os.getpid()))
        self.assertTrue(self.runs(dict(self.env, BMD_ALLOW_DURING_INSTALL='1')))      # setup's own check
        self.lock.write_text('99999999')                                              # setup died
        self.assertTrue(self.runs())
        self.lock.write_text('not a pid')
        self.assertTrue(self.runs())
        self.lock.write_text(str(os.getpid()))
        old = time.time() - 7200                                                      # abandoned lock
        os.utime(self.lock, (old, old))
        self.assertTrue(self.runs())
        self.lock.unlink()
        self.assertTrue(self.runs())


class InstallTests(InstallBase):
    def test_fresh_install_records_ownership_and_registers_host(self):
        manifest = self.install()
        app = self.layout.app
        for rel in ('extension/manifest.json', 'tools/yt-dlp.exe', 'tools/deno.exe', 'tools/ffmpeg.exe',
                    'tools/ffprobe.exe', 'python/python.exe', 'bridge/host-paths.txt',
                    'bridge/com.bravemusic.downloader.json'):
            self.assertTrue((app / rel).exists(), rel)
        self.assertIn('..\\helper', (app / 'python' / 'python312._pth').read_text())
        self.assertEqual(self.leftovers(), [])
        self.assertFalse(self.layout.downloads.exists())
        saved = json.loads(self.layout.manifest.read_text())
        self.assertEqual(saved['version'], '2.5.0')
        owned = {c['name']: c for c in saved['components']}
        self.assertTrue(all(c['owned'] for c in owned.values()))
        self.assertEqual({k: v['verified_by'] for k, v in owned.items()},
                         {'python': 'pinned', 'yt-dlp': 'publisher', 'deno': 'publisher', 'ffmpeg': 'pinned'})
        host = json.loads((app / 'bridge' / 'com.bravemusic.downloader.json').read_text())
        self.assertEqual(host['allowed_origins'], [f'chrome-extension://{EXT_ID}/'])
        self.assertEqual(host['path'], str(self.layout.launcher))
        self.assertEqual((app / 'bridge' / 'host-paths.txt').read_text().split('\n')[0], str(app / 'python' / 'python.exe'))
        for key in ic.BROWSER_KEYS.values():
            self.assertEqual(self.registry.get(key), str(self.layout.native_manifest))
        self.assertEqual((self.layout.output / 'song.wav').read_bytes(), b'MUSIC')

    def test_upgrade_replaces_old_app_and_keeps_user_data(self):
        self.make_old_app()
        old_value = r'C:\old\manifest.json'
        for key in ic.BROWSER_KEYS.values():
            self.registry.set(key, old_value)
        self.install()
        self.assertNotEqual((self.layout.app / 'helper' / 'native_host.py').read_text(), 'old')
        self.assertEqual(self.leftovers(), [])
        self.assertEqual((self.layout.data_dir / 'queue.sqlite3').read_bytes(), b'HISTORY')

    def test_a_2_3_install_with_the_legacy_extension_name_still_upgrades(self):
        """Regression (audit): 2.4.0 renamed the extension from "Brave Music Downloader (WAV)"
        to "Brave Music Downloader". A previous install carrying the legacy name must still be
        recognized as ours, or setup would refuse to replace it and break every upgrade."""
        app = self.layout.app
        legacy_manifest = json.loads(zipfile.ZipFile(self.payload).read('extension/manifest.json'))
        legacy_manifest['name'] = 'Brave Music Downloader (WAV)'
        (app / 'extension').mkdir(parents=True)
        (app / 'extension' / 'manifest.json').write_text(json.dumps(legacy_manifest), encoding='utf-8')
        (app / 'bridge').mkdir()
        (app / 'bridge' / 'host-paths.txt').write_text('old')
        self.assertTrue(ic.recognized_app(app), 'the legacy-named install is recognized')
        self.install()
        self.assertEqual(json.loads((app / 'extension' / 'manifest.json').read_text())['name'],
                         'Brave Music Downloader', 'upgraded to the new name')

    def test_smart_update_reuses_unchanged_components_without_downloading(self):
        """A second install with the same pinned hashes copies the tools from the previous app
        and does not hit the download server for them; only the app payload (extension/helper)
        is replaced from the new zip."""
        first = self.install()
        # The first install downloaded every tool once.
        tool_hits_after_first = [h for h in self.server.hits if h in ('/yt-dlp.exe', '/deno.zip', '/ffmpeg.zip', '/py.zip')]
        self.assertTrue(tool_hits_after_first, 'first install downloaded the tools')
        # The committed manifest records each component's sha256 — the basis for the diff.
        self.assertTrue(all(c.get('sha256') for c in first['components'] if c.get('owned')))
        self.assertEqual(first['update']['fresh_install'], True)
        self.assertEqual(first['update']['reused'], [])
        # Second install over the same tree: same lock, same hashes → everything is reused.
        self.server.hits.clear()
        second = self.install()
        tool_hits_after_second = [h for h in self.server.hits if h in ('/yt-dlp.exe', '/deno.zip', '/ffmpeg.zip', '/py.zip')]
        self.assertEqual(tool_hits_after_second, [], 'no tool was re-downloaded on update')
        self.assertEqual(set(second['update']['reused']), {'python', 'yt-dlp', 'deno', 'ffmpeg'})
        self.assertEqual(second['update']['repaired'], [])
        self.assertEqual(second['update']['fresh_install'], False)
        # The tools in the new app are byte-identical to the original archives (copied, not re-extracted).
        self.assertEqual((self.layout.app / 'tools' / 'yt-dlp.exe').read_bytes(), b'YTDLP-EXE' * 1000)

    def test_smart_update_repairs_a_component_whose_hash_changed(self):
        """If a tool's pinned sha256 changed in the lock, the previous component is re-downloaded
        (repaired) instead of reused. A tool whose file went missing is also repaired."""
        first = self.install()
        # Rewrite the prior manifest so yt-dlp's recorded hash no longer matches the lock.
        manifest = json.loads(self.layout.manifest.read_text())
        for comp in manifest['components']:
            if comp['name'] == 'yt-dlp':
                comp['sha256'] = '0' * 64   # pretend a different version was installed
        ic.bmdfs.write_json_atomic(self.layout.manifest, manifest)
        # Also delete deno's file to force a repair-by-missing.
        (self.layout.app / 'tools' / 'deno.exe').unlink()
        self.server.hits.clear()
        second = self.install()
        self.assertIn('yt-dlp', second['update']['repaired'], 'changed yt-dlp was reinstalled')
        self.assertIn('deno', second['update']['repaired'], 'missing deno was reinstalled')
        self.assertIn('ffmpeg', second['update']['reused'], 'unchanged ffmpeg was reused')
        self.assertIn('python', second['update']['reused'], 'unchanged python was reused')
        # yt-dlp and deno were re-downloaded; ffmpeg and python were not.
        re_downloaded = {h for h in self.server.hits if h in ('/yt-dlp.exe', '/deno.zip')}
        self.assertEqual(re_downloaded, {'/yt-dlp.exe', '/deno.zip'})
        self.assertNotIn('/ffmpeg.zip', self.server.hits)

    def test_any_failure_before_commit_restores_the_previous_install_exactly(self):
        for stage in ('staged', 'before_swap', 'between_renames', 'after_swap', 'after_registry', 'before_commit'):
            with self.subTest(stage=stage):
                shutil.rmtree(self.layout.root, ignore_errors=True)
                self.registry = ic.FakeRegistry()
                self.make_old_app()
                ic.bmdfs.write_json_atomic(self.layout.manifest, {'product': 'BraveMusicDownloader', 'version': 'old'})
                before = self.snapshot()
                prev = r'C:\old\manifest.json'
                self.registry.set(ic.BROWSER_KEYS['Brave'], prev)

                def boom():
                    raise ic.InstallError('injected failure at ' + stage)

                with self.assertRaisesRegex(ic.InstallError, 'injected'):
                    self.install(hooks={stage: boom})
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.registry.get(ic.BROWSER_KEYS['Brave']), prev)
                self.assertIsNone(self.registry.get(ic.BROWSER_KEYS['Chrome']))

    def test_failure_on_a_fresh_machine_leaves_nothing_behind(self):
        for stage in ('staged', 'after_swap', 'after_registry'):
            with self.subTest(stage=stage):
                shutil.rmtree(self.layout.root, ignore_errors=True)
                self.registry = ic.FakeRegistry()
                with self.assertRaises(ic.InstallError):
                    self.install(hooks={stage: lambda: (_ for _ in ()).throw(ic.InstallError('x'))})
                self.assertFalse(self.layout.app.exists())
                self.assertFalse(self.layout.manifest.exists())
                self.assertEqual(self.registry.keys, {})
                self.assertEqual(self.leftovers(), [])

    def test_power_loss_at_any_point_is_recovered_by_the_next_run(self):
        for stage in ('staged', 'between_renames', 'after_swap', 'after_registry', 'before_commit'):
            with self.subTest(stage=stage):
                shutil.rmtree(self.layout.root, ignore_errors=True)
                self.registry = ic.FakeRegistry()
                self.make_old_app()
                before = tree_snapshot(self.layout.app)

                def crash():
                    raise ic.SimulatedCrash()

                with self.assertRaises(ic.SimulatedCrash):
                    self.install(hooks={stage: crash})
                (self.layout.lockfile).unlink(missing_ok=True)    # the dead process's lock
                self.assertTrue(self.layout.journal.exists())
                self.assertEqual(ic.recover_interrupted(self.layout, self.registry), 'rolled back')
                self.assertEqual(tree_snapshot(self.layout.app), before)
                self.assertEqual(self.registry.keys, {})
                self.assertFalse(self.layout.journal.exists())
                self.install()                                     # and a normal install then works
                self.assertTrue(self.layout.manifest.exists())

    def test_crash_after_commit_point_rolls_forward(self):
        self.make_old_app()
        real = ic.finish_committed

        def crash_in_finish(*a, **k):
            raise ic.SimulatedCrash()

        with mock.patch.object(ic, 'finish_committed', crash_in_finish):
            with self.assertRaises(ic.SimulatedCrash):
                self.install()
        self.layout.lockfile.unlink(missing_ok=True)
        self.assertEqual(ic.recover_interrupted(self.layout, self.registry), 'finished')
        self.assertEqual(json.loads(self.layout.manifest.read_text())['version'], '2.5.0')
        self.assertEqual(self.leftovers(), [])

    def test_bad_download_checksum_aborts_without_touching_the_old_app(self):
        self.make_old_app()
        before = tree_snapshot(self.layout.app)
        self.server.routes['/deno.zip'] = zip_bytes({'deno.exe': b'EVIL'})
        with self.assertRaisesRegex(ic.InstallError, 'Checksum mismatch'):
            self.install()
        self.assertEqual(tree_snapshot(self.layout.app), before)
        self.assertEqual(self.registry.keys, {})

    def test_an_online_install_without_a_network_fails_as_a_download_and_installs_nothing(self):
        """The online installer on a PC that is offline: the first tool download is tried three
        times and given up, the failure is of the download category (exit code 4), a previous
        installation is exactly as it was and a fresh machine stays fresh."""
        calls = []

        def offline(request, timeout=0):
            calls.append(request.full_url)
            raise urllib.error.URLError('[Errno 111] Connection refused')

        with mock.patch.object(ic.time, 'sleep') as sleep:         # no waiting between the tries
            self.make_old_app()
            ic.bmdfs.write_json_atomic(self.layout.manifest, {'product': 'BraveMusicDownloader', 'version': 'old'})
            before = self.snapshot()
            previous = r'C:\old\manifest.json'
            self.registry.set(ic.BROWSER_KEYS['Brave'], previous)
            with self.assertRaises(ic.DownloadFailed) as raised:
                self.install(opener=offline)
            self.assertIn('Check your internet connection and run setup again', str(raised.exception))
            self.assertEqual(raised.exception.category, 'download')
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(self.registry.get(ic.BROWSER_KEYS['Brave']), previous)
            self.assertEqual(self.leftovers(), [])
            self.assertEqual(len(calls), 3, 'the first tool is tried three times, no other is tried')
            self.assertEqual(len(set(calls)), 1, calls)
            kept = sorted(p.relative_to(self.layout.root).as_posix() for p in self.layout.downloads.rglob('*'))
            self.assertEqual(kept, [], 'nothing half-downloaded is kept')

            shutil.rmtree(self.layout.root, ignore_errors=True)
            self.registry = ic.FakeRegistry()
            calls.clear()
            with self.assertRaises(ic.DownloadFailed):
                self.install(opener=offline)
            self.assertFalse(self.layout.app.exists())
            self.assertFalse(self.layout.manifest.exists())
            self.assertEqual(self.registry.keys, {})
            self.assertEqual(self.leftovers(), [])
        self.assertEqual(sleep.call_args_list, [mock.call(1.0), mock.call(2.0)] * 2,
                         'a longer pause between the tries, none after the last one')

    def test_refuses_foreign_app_folder_and_wrong_extension_identity(self):
        (self.layout.app).mkdir(parents=True)
        (self.layout.app / 'mine.txt').write_text('not yours')
        with self.assertRaisesRegex(ic.InstallError, 'not a Brave Music Downloader folder'):
            self.install()
        self.assertEqual((self.layout.app / 'mine.txt').read_text(), 'not yours')
        shutil.rmtree(self.layout.app)
        with self.assertRaisesRegex(ic.InstallError, 'identity check'):
            ic.install(self.layout, self.payload, self.lock, self.registry, extension_id='a' * 32)
        self.assertEqual(self.registry.keys, {})

    def test_incomplete_payload_is_rejected(self):
        self.payload = make_payload(self.tmp, drop=('helper/native_host.py',))
        with self.assertRaisesRegex(ic.InstallError, 'missing helper/native_host.py'):
            self.install()
        self.assertFalse(self.layout.app.exists())

    def test_single_instance_lock(self):
        self.layout.root.mkdir(parents=True, exist_ok=True)
        self.layout.lockfile.write_text(str(os.getpid()))          # a live process holds it
        with self.assertRaisesRegex(ic.InstallError, 'already running'):
            self.install()
        self.layout.lockfile.write_text('999999')                   # dead process: taken over
        self.install()
        self.assertTrue(self.layout.manifest.exists())

    def test_existing_system_tools_can_be_reused_and_are_not_owned(self):
        fake_bin = self.tmp / 'bin'
        fake_bin.mkdir()
        fake = fake_bin / ('ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')
        fake.write_text('x')                       # shutil.which must find it; nothing runs it
        if os.name != 'nt':
            fake.chmod(0o755)
        with mock.patch.dict(os.environ, {'PATH': str(fake_bin)}):
            manifest = self.install(reuse_system=True)
        comps = {c['name']: c for c in manifest['components']}
        self.assertFalse(comps['ffmpeg']['owned'])
        self.assertTrue(comps['yt-dlp']['owned'])
        self.assertFalse((self.layout.app / 'tools' / 'ffmpeg.exe').exists())


class UninstallTests(InstallBase):
    def setUp(self):
        super().setUp()
        self.install()
        (self.layout.cache / 'jobs' / ('a' * 32)).mkdir(parents=True)
        (self.layout.cache / 'jobs' / ('a' * 32) / 'source.m4a').write_bytes(b'partial')
        (self.layout.data_dir / 'musicbrainz-cache').mkdir()
        (self.layout.data_dir / 'access_token.txt').write_text('secret')

    def test_default_uninstall_keeps_music_settings_and_history_and_removes_cache(self):
        report = ic.uninstall(self.layout, self.registry)
        self.assertEqual(report['failed'], [])
        self.assertFalse(self.layout.app.exists())
        self.assertFalse(self.layout.cache.exists())
        self.assertFalse(self.layout.manifest.exists())
        self.assertEqual(self.registry.keys, {})
        self.assertEqual((self.layout.output / 'song.wav').read_bytes(), b'MUSIC')
        self.assertEqual((self.layout.data_dir / 'queue.sqlite3').read_bytes(), b'HISTORY')
        self.assertFalse((self.layout.data_dir / 'access_token.txt').exists())
        self.assertFalse((self.layout.data_dir / 'musicbrainz-cache').exists())

    def test_registry_cleanup_is_conditional_on_ownership(self):
        other = r'D:\other-product\host.json'
        self.registry.set(ic.BROWSER_KEYS['Chrome'], other)
        report = ic.uninstall(self.layout, self.registry)
        self.assertEqual(self.registry.get(ic.BROWSER_KEYS['Chrome']), other)
        self.assertIsNone(self.registry.get(ic.BROWSER_KEYS['Brave']))
        self.assertTrue(any('left alone' in line for line in report['skipped']))

    def test_files_the_app_did_not_create_survive(self):
        (self.layout.app / 'notes.txt').write_text('my notes')
        (self.layout.app / 'tools' / 'custom.exe').write_text('my tool')
        report = ic.uninstall(self.layout, self.registry)
        self.assertEqual((self.layout.app / 'notes.txt').read_text(), 'my notes')
        self.assertEqual((self.layout.app / 'tools' / 'custom.exe').read_text(), 'my tool')
        self.assertFalse((self.layout.app / 'tools' / 'yt-dlp.exe').exists())
        self.assertFalse((self.layout.app / 'python').exists())
        self.assertTrue(any('did not create' in line for line in report['kept']))
        self.assertTrue(self.layout.manifest.parent.exists())

    def test_tampered_manifest_cannot_delete_outside_the_app_folder(self):
        victim = self.tmp / 'victim'
        victim.mkdir()
        (victim / 'important.txt').write_text('precious')
        (self.layout.root.parent / 'sibling.txt').write_text('precious')
        manifest = json.loads(self.layout.manifest.read_text())
        manifest['components'].append({'name': 'evil', 'owned': True, 'path': '../../../victim',
                                      'files': ['../../../victim', 'tools/../../../victim', '/etc/passwd',
                                                'C:/Windows/notepad.exe']})
        manifest['owned_entries'] += ['../../victim', '..', 'Music']
        ic.bmdfs.write_json_atomic(self.layout.manifest, manifest)
        os.symlink(victim, self.layout.app / 'extension' / 'jump')
        ic.uninstall(self.layout, self.registry)
        self.assertEqual((victim / 'important.txt').read_text(), 'precious')
        self.assertEqual((self.layout.root.parent / 'sibling.txt').read_text(), 'precious')
        self.assertEqual((self.layout.output / 'song.wav').read_bytes(), b'MUSIC')

    def test_dependencies_not_owned_by_the_app_are_never_removed(self):
        manifest = json.loads(self.layout.manifest.read_text())
        for comp in manifest['components']:
            if comp['name'] == 'ffmpeg':
                comp['owned'] = False
        ic.bmdfs.write_json_atomic(self.layout.manifest, manifest)
        report = ic.uninstall(self.layout, self.registry)
        self.assertTrue((self.layout.app / 'tools' / 'ffmpeg.exe').exists())
        self.assertTrue(any(line.startswith('ffmpeg') for line in report['kept']))

    def test_history_removal_needs_confirmation_and_never_touches_music(self):
        with self.assertRaisesRegex(ic.InstallError, 'DELETE HISTORY'):
            ic.uninstall(self.layout, self.registry, remove_history=True)
        self.assertTrue((self.layout.data_dir / 'queue.sqlite3').exists())
        ic.uninstall(self.layout, self.registry, remove_history=True, confirm='DELETE HISTORY')
        self.assertFalse((self.layout.data_dir / 'queue.sqlite3').exists())
        self.assertEqual((self.layout.output / 'song.wav').read_bytes(), b'MUSIC')

    def test_dry_run_changes_nothing(self):
        before = tree_snapshot(self.tmp)
        keys = dict(self.registry.keys)
        report = ic.uninstall(self.layout, self.registry, dry_run=True)
        self.assertEqual(tree_snapshot(self.tmp), before)
        self.assertEqual(self.registry.keys, keys)
        self.assertTrue(report['removed'] and all(x.startswith('[dry run]') for x in report['removed']))

    def test_interrupted_uninstall_can_simply_be_run_again(self):
        real = ic.bmdfs.safe_rmtree
        calls = {'n': 0}

        def flaky(path, boundary):
            if Path(path).name == 'python' and calls['n'] == 0:
                calls['n'] += 1
                raise PermissionError('python.exe is in use')
            return real(path, boundary)

        with mock.patch.object(ic.bmdfs, 'safe_rmtree', flaky):
            first = ic.uninstall(self.layout, self.registry)
        self.assertTrue(first['failed'])
        self.assertTrue(self.layout.manifest.exists(), 'ownership record is kept until everything is removed')
        second = ic.uninstall(self.layout, self.registry)
        self.assertEqual(second['failed'], [])
        self.assertFalse(self.layout.app.exists())
        self.assertFalse(self.layout.manifest.exists())

    def test_pre_2_3_install_without_a_record_is_uninstalled_by_layout(self):
        self.layout.manifest.unlink()
        report = ic.uninstall(self.layout, self.registry)
        self.assertTrue(report['legacy_install'])
        self.assertFalse(self.layout.app.exists())
        self.assertEqual(self.registry.keys, {})
        self.assertEqual((self.layout.data_dir / 'queue.sqlite3').read_bytes(), b'HISTORY')

    def test_a_running_uninstall_script_does_not_fail_the_uninstall(self):
        real = ic.bmdfs.safe_unlink

        def locked(path, boundary):
            if Path(path).name == 'Uninstall.ps1':
                raise PermissionError('being used by another process')
            return real(path, boundary)

        with mock.patch.object(ic.bmdfs, 'safe_unlink', locked):
            report = ic.uninstall(self.layout, self.registry)
        self.assertEqual(report['failed'], [])
        self.assertTrue(any('Uninstall.ps1' in line for line in report['kept']))
        self.assertTrue((self.layout.app / 'Uninstall.ps1').exists())
        self.assertFalse(self.layout.manifest.exists())
        self.assertFalse((self.layout.app / 'python').exists())

    def test_python_signature_only_components_cannot_be_installed_without_setup(self):
        lock = {'components': [{'name': 'python', 'url': self.server.base + '/py.zip', 'kind': 'zip',
                                'extract_to': 'python', 'provides': ['python/python.exe'],
                                'authenticode': 'Python Software Foundation'}]}
        shutil.rmtree(self.layout.root)
        self.registry = ic.FakeRegistry()
        with self.assertRaisesRegex(ic.InstallError, 'only be verified by Setup.ps1'):
            ic.install(self.layout, self.payload, lock, self.registry, extension_id=EXT_ID, rename_retries=1)
        self.assertEqual(self.registry.keys, {})

    def test_a_custom_music_folder_with_spaces_and_non_ascii_outside_the_app_is_untouched(self):
        music = self.tmp / 'Mûsica  ñ (copia) 日本語'
        (music / 'Artista' / 'Álbum').mkdir(parents=True)
        (music / 'Artista' / 'Álbum' / 'Canción [abcdefghijk].wav').write_bytes(b'MUSIC')
        self.layout.output = music
        before = tree_snapshot(music)
        report = ic.uninstall(self.layout, self.registry)
        self.assertEqual(report['failed'], [])
        self.assertEqual(tree_snapshot(music), before)
        self.assertTrue(any(str(music) in line for line in report['kept']))

    def test_a_music_folder_inside_the_install_root_survives_and_keeps_the_root(self):
        music = self.layout.root / 'My Music Library'
        music.mkdir()
        (music / 'keep me.wav').write_bytes(b'MUSIC')
        self.layout.output = music
        ic.uninstall(self.layout, self.registry)
        self.assertEqual((music / 'keep me.wav').read_bytes(), b'MUSIC')
        self.assertFalse(self.layout.app.exists())

    @unittest.expectedFailure
    def test_KNOWN_GAP_a_music_folder_inside_the_cache_is_not_protected_yet(self):
        # The uninstaller removes the whole cache folder. If a custom music folder were placed inside it,
        # the music would go too. 2.3 has no setting for this (only the BMD_OUTPUT variable), and changing
        # deletion logic needs approval; this test records the gap and starts passing once it is closed.
        music = self.layout.cache / 'music'
        music.mkdir(parents=True)
        (music / 'keep me.wav').write_bytes(b'MUSIC')
        self.layout.output = music
        ic.uninstall(self.layout, self.registry)
        self.assertTrue((music / 'keep me.wav').exists())

    def test_nothing_installed_means_nothing_deleted(self):
        other = ic.Layout(self.tmp / 'Elsewhere' / 'BMD', self.tmp / 'Roaming' / 'BMD', self.tmp / 'Music')
        (other.app).mkdir(parents=True)
        (other.app / 'foreign.txt').write_text('x')
        report = ic.uninstall(other, ic.FakeRegistry())
        self.assertEqual((other.app / 'foreign.txt').read_text(), 'x')
        self.assertTrue(any('left alone' in s for s in report['skipped']))

    def test_uninstall_finishes_an_interrupted_install_first(self):
        shutil.rmtree(self.layout.root)
        self.registry = ic.FakeRegistry()
        self.make_old_app()
        with self.assertRaises(ic.SimulatedCrash):
            self.install(hooks={'after_swap': lambda: (_ for _ in ()).throw(ic.SimulatedCrash())})
        self.layout.lockfile.unlink(missing_ok=True)
        report = ic.uninstall(self.layout, self.registry)
        self.assertEqual(report['failed'], [])
        self.assertEqual(self.registry.keys, {})


class ErrorReportingTests(InstallBase):
    def test_unexpected_exceptions_get_a_plain_message_and_the_traceback_goes_to_the_log(self):
        out = io.StringIO()
        with mock.patch.object(ic, 'install', side_effect=RuntimeError('boom')), contextlib.redirect_stdout(out):
            code = ic.main(['install', '--payload', str(self.payload), '--root', str(self.layout.root),
                            '--extension-id', EXT_ID, '--registry', 'fake'])
        text = out.getvalue()
        self.assertEqual(code, 1)
        self.assertIn('ERROR: Setup hit an unexpected problem (RuntimeError: boom)', text)
        self.assertIn('restored as it was', text)
        self.assertIn('Traceback', text)                       # kept for the log, after the plain message
        self.assertLess(text.index('ERROR:'), text.index('Traceback'))

    def test_rollback_wording_distinguishes_a_failed_run_from_a_crashed_one(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(ic.InstallError):
                self.install(hooks={'staged': lambda: (_ for _ in ()).throw(ic.InstallError('x'))})
        self.assertIn('Setup did not finish; restoring the previous state', out.getvalue())
        self.assertNotIn('interrupted', out.getvalue())
        out = io.StringIO()
        with self.assertRaises(ic.SimulatedCrash):
            self.install(hooks={'after_swap': lambda: (_ for _ in ()).throw(ic.SimulatedCrash())})
        self.layout.lockfile.unlink(missing_ok=True)
        with contextlib.redirect_stdout(out):
            ic.recover_interrupted(self.layout, self.registry)
        self.assertIn('Rolling back the interrupted installation', out.getvalue())


if __name__ == '__main__':
    unittest.main()
