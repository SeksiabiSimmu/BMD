"""Run the real setup launcher under Wine: the Windows program built from setup_launcher.c.

The portable core is compiled and tested on this machine (test_launcher_native.py), but the
Windows half - the command line, the work folder, the verified unpacking, the hand-over to
PowerShell and the exit codes - only exists on Windows. Wine runs the real .exe here; the program
it starts is tests/native/stub_powershell.c, put where the launcher expects Windows PowerShell and
removed again afterwards.

What this cannot show: how the window looks, and anything about real Windows PowerShell. Those
steps are in WINDOWS_QA.md.

Needs Zig (ZIG=/path/to/zig, or `zig` on the PATH) and Wine (WINE=<wine binary or wrapper script>,
or `wine64`/`wine` on the PATH; WINE_PREFIX=<prefix folder>, default ~/.wine). Without either,
every test in this module is skipped.
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from installer_support import fake_sign, zip_bytes
from support import ROOT, tmpdir

import payload_container as pc

sys.path.insert(0, str(ROOT / 'scripts'))
import build_launcher as bl   # noqa: E402

LAUNCHER = ROOT / 'app' / 'installer' / 'launcher'
STUB = ROOT / 'tests' / 'native' / 'stub_powershell.c'


def tools():
    """(zig or None, wine command line or None, prefix folder)."""
    zig = os.environ.get('ZIG') or shutil.which('zig')
    wine = os.environ.get('WINE') or shutil.which('wine64') or shutil.which('wine')
    prefix = Path(os.environ.get('WINE_PREFIX') or Path.home() / '.wine')
    if not wine:
        return zig, None, prefix
    if os.access(wine, os.X_OK):
        return zig, [wine], prefix
    with open(wine, 'rb') as handle:
        shebang = handle.read(2) == b'#!'
    return zig, (['bash', wine] if shebang else None), prefix


ZIG, WINE, PREFIX = tools()
POWERSHELL = PREFIX / 'drive_c' / 'windows' / 'system32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
BACKUP = POWERSHELL.with_name('powershell.exe.bmd-test-backup')


def windows(path):
    """A POSIX path as Wine sees it (drive Z: is the root folder)."""
    return 'Z:' + str(path).replace('/', '\\')


def posix(path, prefix):
    """A Wine drive path back to a POSIX path."""
    drive, rest = path[:2].lower(), path[3:].replace('\\', '/')
    if drive == 'z:':
        return Path('/') / rest
    return prefix / 'drive_c' / rest


def build_stub(zig, output):
    """tests/native/stub_powershell.c -> a console program for Windows."""
    zig = bl.find_zig(zig)
    env = dict(os.environ, ZIG_LOCAL_CACHE_DIR=str(ROOT / '.zig-cache'))
    bl.run([zig, 'cc', '-target', 'x86_64-windows-gnu', '-std=c11', '-O1', '-nostdlib',
            '-isystem', bl.windows_headers(zig), '-I', LAUNCHER, '-DUNICODE', '-D_UNICODE',
            '-Wall', '-Wextra', '-Werror', '-Wl,-e,mainCRTStartup', '-s', '-o', output,
            STUB, LAUNCHER / 'launcher_core.c', '-lkernel32', '-lshell32'], env=env)


def is_stub(path):
    """Whether the file is the test stub, whatever built it: only the stub looks for BMD_STUB_*."""
    return 'BMD_STUB'.encode('utf-16-le') in path.read_bytes()


def record(path):
    """The stub's record: {kind: [value, ...]}."""
    facts = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        kind, _, value = line.partition('|')
        facts.setdefault(kind, []).append(value)
    return facts


def leftover_work_folders(prefix):
    """Work folders (%TEMP%\\BMD-Setup-*) that still exist in the Wine prefix."""
    return sorted((prefix / 'drive_c' / 'users').glob('*/AppData/Local/Temp/BMD-Setup-*'))


class LauncherUnderWine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not ZIG or not WINE:
            raise unittest.SkipTest('needs Zig (ZIG=...) and Wine (WINE=...) to run the Windows launcher')
        cls.folder = tmpdir()
        cls.image = cls.folder / 'image' / 'BraveMusicDownloader-Setup.exe'
        bl.build(ZIG, cls.image)                                  # fails the test when the sources do not build
        cls.stub = cls.folder / 'stub.exe'
        build_stub(ZIG, cls.stub)
        # Wine creates the folder on the first start; then put the stub where the launcher looks.
        subprocess.run(WINE + ['cmd', '/c', 'exit', '0'], capture_output=True, timeout=240,
                       stdin=subprocess.DEVNULL)
        for stale in leftover_work_folders(PREFIX):             # what an interrupted earlier run left
            shutil.rmtree(stale, ignore_errors=True)
        POWERSHELL.parent.mkdir(parents=True, exist_ok=True)
        # Only another program's file is put back later. Our own stub, left by a run that was killed,
        # must not be kept as if it were Wine's: it answers every setup run with exit code 0.
        if POWERSHELL.exists() and not is_stub(POWERSHELL):
            shutil.copyfile(POWERSHELL, BACKUP)                  # Wine's own placeholder: put it back later
        shutil.copyfile(cls.stub, POWERSHELL)

    @classmethod
    def tearDownClass(cls):
        if not WINE:
            return
        if BACKUP.exists() and not is_stub(BACKUP):
            shutil.move(str(BACKUP), str(POWERSHELL))
        else:
            POWERSHELL.unlink(missing_ok=True)
            BACKUP.unlink(missing_ok=True)
        shutil.rmtree(cls.folder, ignore_errors=True)

    def run_launcher(self, exe, *args, env=None):
        merged = dict(os.environ, **(env or {}))
        run = subprocess.run(WINE + [windows(exe), *args], capture_output=True, timeout=240,
                             stdin=subprocess.DEVNULL, env=merged)
        run.text = run.stdout.decode('utf-8', 'replace')
        return run

    def make_installer(self, name='Setup.exe', python=True, tools=True, damage=None):
        """An installer file in the test folder. Returns (path, entries).

        `python` is the bundled Python archive (bootstrap), `tools` a file that the launcher leaves
        in the installer file (a tool archive of the offline build, read by the engine).
        """
        entries = [('Setup.ps1', b'<# setup script #>\n' * 40, True),
                   ('Application.zip', zip_bytes({'README.md': 'Brave Music Downloader\n' * 200}), True)]
        if python:
            entries.append(('python', zip_bytes({'python.exe': b'PY' * 4000}), True))
        if tools:
            entries.append(('ffmpeg', bytes(range(256)) * 800, False))
        data = bytearray(pc.build(self.image.read_bytes(), entries))
        if damage:
            damage(data, pc.parse(bytes(data)))
        path = self.folder / name
        path.write_bytes(bytes(data))
        return path, entries

    def stub_record(self, name='record.txt'):
        """(Windows path of a new record file, the POSIX path)."""
        path = self.folder / name
        path.unlink(missing_ok=True)
        return windows(path), path

    def test_the_self_test_reports_every_file_and_passes(self):
        path, entries = self.make_installer()
        run = self.run_launcher(path, '/SELFTEST')
        self.assertEqual(run.returncode, 0, run.text)
        for entry_name, data, _ in entries:
            self.assertRegex(run.text, rf'(?m)^ok\s+{re.escape(entry_name)}\s+{len(data)} bytes\r?$',
                             'one line per file with its name and size')
        self.assertIn('Self-test passed: all 4 files match their SHA-256.', run.text)
        self.assertNotIn('FAILED', run.text)

    def test_a_damaged_entry_fails_the_self_test_and_nothing_is_started(self):
        def flip(index):
            """Damage entry number `index`: one byte of its data is inverted."""
            def damage(data, parsed):
                at = parsed['entries'][index]['offset'] + 100
                data[at] ^= 0xFF
            return damage
        path, _ = self.make_installer(name='Damaged.exe', damage=flip(3))     # ffmpeg
        run = self.run_launcher(path, '/SELFTEST')
        self.assertEqual(run.returncode, 3, run.text)             # 3: the installer file is damaged
        self.assertRegex(run.text, r'(?m)^FAILED\s+ffmpeg\s+\d+ bytes\s+- a file inside the installer does not'
                                    r' match its checksum\r?$')
        self.assertIn('1 of 4 files do not match', run.text)
        for index, name in ((0, 'Setup.ps1'), (1, 'Application.zip'), (2, 'python')):
            path, _ = self.make_installer(name=f'Broken{index}.exe', damage=flip(index))
            out, record_path = self.stub_record(f'broken{index}.txt')
            run = self.run_launcher(path, env={'BMD_STUB_OUT': out})
            self.assertEqual(run.returncode, 3, run.text)
            self.assertIn(f'could not be unpacked: {name}', run.text)
            self.assertFalse(record_path.exists(), 'nothing from a damaged installer may be started')
        self.assertEqual(leftover_work_folders(PREFIX), [], 'the work folders are removed after a failure too')
        # Files the script does not need to start (here ffmpeg) are unpacked and verified by the script
        # itself, one by one (payload_container.read); tests/test_container.py covers that step.

    def test_a_truncated_installer_is_refused_before_anything_runs(self):
        path, _ = self.make_installer(name='Cut.exe')
        path.write_bytes(path.read_bytes()[:-10])               # the footer is gone: nothing identifies the file
        out, record_path = self.stub_record('cut.txt')
        run = self.run_launcher(path, '/SELFTEST', env={'BMD_STUB_OUT': out})
        self.assertEqual(run.returncode, 11, run.text)
        self.assertIn('This file is not a Brave Music Downloader installer.', run.text)
        self.assertIn('Download the installer again', run.text)
        run = self.run_launcher(path, '/S', env={'BMD_STUB_OUT': out})
        self.assertEqual(run.returncode, 11, run.text)
        self.assertFalse(record_path.exists(), 'nothing from an unidentifiable file may be started')

    def test_a_signed_installer_with_the_signing_tool_s_padding_passes_and_any_other_trailing_byte_fails(self):
        entries = [('Setup.ps1', b'<# setup script #>\n' * 40, True),
                   ('Application.zip', zip_bytes({'README.md': 'Brave Music Downloader\n' * 200}), True),
                   ('python', zip_bytes({'python.exe': b'PY' * 4000}), True)]
        tool = bytes(range(256)) * 800
        for extra in (b'', b'!'):                      # one of the two lengths is not a multiple of 8
            data = pc.build(self.image.read_bytes(), entries + [('ffmpeg', tool + extra, False)])
            if len(data) % 8:
                break
        self.assertNotEqual(len(data) % 8, 0)
        signed = fake_sign(data)                       # zeros up to the 8-byte boundary, then the table
        self.assertEqual(len(signed) - len(data), 8 - len(data) % 8 + 264)
        path = self.folder / 'Signed.exe'
        path.write_bytes(signed)
        run = self.run_launcher(path, '/SELFTEST')
        self.assertEqual(run.returncode, 0, run.text)
        self.assertIn('Self-test passed: all 4 files match their SHA-256.', run.text)
        run = self.run_launcher(path, '/EXTRACT=' + windows(self.folder / 'signed-extract'))
        self.assertEqual(run.returncode, 0, run.text)
        self.assertEqual((self.folder / 'signed-extract' / 'ffmpeg').read_bytes(), tool + extra)
        spoiled = bytearray(signed)
        spoiled[len(data)] ^= 0xFF                     # the first byte after the footer is no longer zero
        path.write_bytes(spoiled)
        out, record_path = self.stub_record('padding.txt')
        run = self.run_launcher(path, '/SELFTEST', env={'BMD_STUB_OUT': out})
        self.assertEqual(run.returncode, 3, run.text)
        self.assertIn('the installer file is damaged or incomplete', run.text)
        run = self.run_launcher(path, '/S', env={'BMD_STUB_OUT': out})
        self.assertEqual(run.returncode, 3, run.text)
        self.assertFalse(record_path.exists(), 'nothing from a file with extra data may be started')

    def test_extract_writes_every_file_and_refuses_a_folder_that_is_in_use(self):
        path, entries = self.make_installer()
        target = self.folder / 'extracted' / 'nested'
        run = self.run_launcher(path, '/EXTRACT=' + windows(target))
        self.assertEqual(run.returncode, 0, run.text)
        self.assertIn('Extracted 4 files into', run.text)
        for entry_name, data, _ in entries:
            self.assertEqual((target / entry_name).read_bytes(), data, entry_name)
        self.assertEqual(sorted(p.name for p in target.iterdir()), sorted(e[0] for e in entries))
        run = self.run_launcher(path, '/EXTRACT=' + windows(target))
        self.assertEqual(run.returncode, 1, run.text)             # 1: a general failure, nothing was overwritten
        self.assertIn('the folder is not empty', run.text)

    def test_help_exits_zero_and_an_unknown_option_exits_ten(self):
        path, _ = self.make_installer()
        run = self.run_launcher(path, '/?')
        self.assertEqual(run.returncode, 0, run.text)
        self.assertIn('Exit codes: 0 done', run.text)
        self.assertIn('/ALLOWUNVERIFIED', run.text)
        run = self.run_launcher(path, '/NOPE')
        self.assertEqual(run.returncode, 10, run.text)
        self.assertTrue(run.text.startswith('Unknown or malformed option: /NOPE\r\n'), run.text)
        self.assertIn('/SELFTEST', run.text)                      # the help follows the error
        run = self.run_launcher(path, '/EXTRACT')
        self.assertEqual(run.returncode, 10, run.text)            # an option that needs a value

    def test_a_file_without_a_container_is_not_an_installer(self):
        bare = self.folder / 'Bare.exe'
        bare.write_bytes(self.image.read_bytes())
        run = self.run_launcher(bare, '/SELFTEST')
        self.assertEqual(run.returncode, 11, run.text)
        self.assertIn('This file is not a Brave Music Downloader installer.', run.text)
        trailing = self.folder / 'Trailing.exe'                  # a program with unexplained bytes behind it
        trailing.write_bytes(self.image.read_bytes() + bytes(range(256)) * 64)
        run = self.run_launcher(trailing, '/SELFTEST')
        self.assertEqual(run.returncode, 11, run.text)
        self.assertIn('This file is not a Brave Music Downloader installer.', run.text)
        # A file that is no Windows program at all is turned away by Windows itself (Wine: exit code 1)
        # before the launcher runs; that this exit code is not 0 is the only thing a test can rely on.
        text = self.folder / 'notes.exe'
        text.write_bytes(b'this is not a program at all' * 20)
        self.assertNotIn(self.run_launcher(text, '/SELFTEST').returncode, (0, 3))

    def test_the_script_is_started_with_the_expected_command_line(self):
        path, entries = self.make_installer()
        out, record_path = self.stub_record()
        run = self.run_launcher(path, env={'BMD_STUB_OUT': out})
        self.assertEqual(run.returncode, 0, run.text)
        self.assertEqual(run.text, '')                            # silent unless something failed
        facts = record(record_path)
        arguments = facts['argument']
        raw = record_path.read_text(encoding='utf-8')
        # %SystemRoot% as Windows reports it, not the Z: path Wine was started from
        self.assertEqual(arguments[0].lower(), r'c:\windows\system32\windowspowershell\v1.0\powershell.exe', raw)
        work = facts['cwd'][0]
        self.assertRegex(work.lower(), r'^c:\\.*\\bmd-setup-\d+-\d+$')   # the private work folder
        self.assertEqual(arguments[1:], [
            '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', work + '\\Setup.ps1',
            '-PayloadZip', work + '\\Application.zip', '-Installer', windows(path), '-Bundle',
            '-PythonZip', work + '\\python', '-Mode', 'Gui', '-Action', 'Install'])
        self.assertEqual(facts['installer'], ['1|' + windows(path)])
        files = dict(line.split('|', 1) for line in facts['file'])     # name -> "size|sha256"
        self.assertEqual(sorted(files), ['Application.zip', 'Setup.ps1', 'python'],
                         'only the bootstrap files are unpacked before the script starts')
        for entry_name, data, bootstrap in entries:
            if bootstrap:
                self.assertEqual(files[entry_name], f'{len(data)}|{hashlib.sha256(data).hexdigest()}')
        self.assertFalse(posix(work, PREFIX).exists(), 'the work folder is removed afterwards')
        self.assertEqual(leftover_work_folders(PREFIX), [], 'no work folder is left behind')

    def test_an_online_installer_passes_no_python_archive(self):
        path, _ = self.make_installer(python=False)
        out, record_path = self.stub_record()
        self.assertEqual(self.run_launcher(path, env={'BMD_STUB_OUT': out}).returncode, 0)
        facts = record(record_path)
        self.assertNotIn('-PythonZip', facts['argument'])
        self.assertEqual(sorted(line.split('|')[0] for line in facts['file']), ['Application.zip', 'Setup.ps1'])

    def test_the_bundle_switch_marks_an_installer_that_keeps_files_for_the_engine(self):
        # -Bundle tells the script that tool archives stay in the installer file and the engine must
        # read them from there (payload_container). Nothing else in the command line depends on it.
        for python, tools, switches in ((True, True, ('-Bundle', '-PythonZip')), (True, False, ('-PythonZip',)),
                                        (False, True, ('-Bundle',)), (False, False, ())):
            path, _ = self.make_installer(name=f'Setup-{int(python)}{int(tools)}.exe', python=python, tools=tools)
            out, record_path = self.stub_record(f'bundle-{int(python)}{int(tools)}.txt')
            self.assertEqual(self.run_launcher(path, env={'BMD_STUB_OUT': out}).returncode, 0)
            arguments = record(record_path)['argument']
            self.assertEqual(tuple(s for s in ('-Bundle', '-PythonZip') if s in arguments), switches,
                             f'python={python} tools={tools}: {arguments}')
            self.assertLess(arguments.index('-Installer'), arguments.index('-Mode'))

    def test_options_are_passed_on_and_the_exit_code_is_returned(self):
        path, _ = self.make_installer()
        out, record_path = self.stub_record()
        log = self.folder / 'with space.log'
        run = self.run_launcher(path, '/S', '/UNINSTALL', '/LOG=' + windows(log), '/ALLOWDOWNGRADE',
                                '/NOSHORTCUTS', '/REMOVEHISTORY', '/ALLOWUNVERIFIED',
                                env={'BMD_STUB_OUT': out, 'BMD_STUB_EXIT': '9'})
        self.assertEqual(run.returncode, 9, run.text)
        self.assertEqual(run.text, '')
        arguments = record(record_path)['argument']
        self.assertEqual(arguments[arguments.index('-Mode'):],
                         ['-Mode', 'Silent', '-Action', 'Uninstall', '-Log', windows(log),
                          '-AllowDowngrade', '-NoShortcuts', '-RemoveHistory', '-AllowUnverified'])

    def test_console_repair_and_diagnose_reach_the_script(self):
        path, _ = self.make_installer()
        out, record_path = self.stub_record()
        self.assertEqual(self.run_launcher(path, '/CONSOLE', '/REPAIR', env={'BMD_STUB_OUT': out}).returncode, 0)
        arguments = record(record_path)['argument']
        self.assertEqual(arguments[arguments.index('-Mode'):], ['-Mode', 'Console', '-Action', 'Repair'])
        out, record_path = self.stub_record('diagnose.txt')
        self.assertEqual(self.run_launcher(path, '/DIAGNOSE', '/REQUIREBRAVE',
                                           env={'BMD_STUB_OUT': out}).returncode, 0)
        arguments = record(record_path)['argument']
        self.assertEqual(arguments[arguments.index('-Mode'):],
                         ['-Mode', 'Gui', '-Action', 'Diagnose', '-RequireBrave'])

    def test_a_failed_download_exit_code_and_a_missing_powershell(self):
        path, _ = self.make_installer()
        out, _ = self.stub_record()
        run = self.run_launcher(path, '/S', env={'BMD_STUB_OUT': out, 'BMD_STUB_EXIT': '4'})
        self.assertEqual(run.returncode, 4, run.text)
        planted = POWERSHELL.read_bytes()
        POWERSHELL.unlink()
        try:
            run = self.run_launcher(path, '/S')
        finally:
            POWERSHELL.write_bytes(planted)
        self.assertEqual(run.returncode, 12, run.text)            # 12: Windows PowerShell is missing
        self.assertIn('Windows PowerShell could not be started', run.text)

    def test_a_script_exit_code_outside_the_table_is_a_failed_setup(self):
        # The script ends with 0..12 (Setup.ps1, exit codes). 99 is what an uncaught PowerShell error
        # or a stopped process can leave behind, 3221225475 (0xC0000005) what a crash leaves behind;
        # neither may reach a caller as if it meant something.
        path, _ = self.make_installer()
        for code in ('99', '3221225475', '13'):
            out, _ = self.stub_record()
            run = self.run_launcher(path, '/S', env={'BMD_STUB_OUT': out, 'BMD_STUB_EXIT': code})
            self.assertEqual(run.returncode, 1, f'exit code {code}: {run.text}')
            self.assertIn(f'Setup did not complete: the setup script ended unexpectedly (exit code {code}).',
                          run.text)
            self.assertIn(r'%LOCALAPPDATA%\BraveMusicDownloader\Install.log', run.text)
            self.assertNotIn('could not be started', run.text)
        self.assertEqual(leftover_work_folders(PREFIX), [], 'the work folder is removed after a failure too')


if __name__ == '__main__':
    unittest.main()
