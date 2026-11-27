"""Setup flow added in 2.5.0: exit categories, downgrade guard, offline bundle, Programs and
Features entry, progress/cancel channel, result file, diagnostics, extension contact."""
import contextlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import unittest
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

from support import ROOT, tmpdir
from installer_support import EXT_ID, fake_components, fake_pe, make_payload, sha, tree_snapshot
from test_installer import InstallBase, REPLY_PRELUDE, script

import bmdfs
import installer_core as ic
import native_host
import payload_container as pc
import paths

TOOLS = ('/yt-dlp.exe', '/deno.zip', '/ffmpeg.zip', '/py.zip')


def pinned(lock, routes):
    """Give every component a pinned sha256 (what an offline installer is built from)."""
    for entry in lock['components']:
        entry.pop('checksum', None)
        entry['sha256'] = sha(routes['/' + entry['url'].split('/', 3)[-1]])
    return lock


class VersionCompareTests(unittest.TestCase):
    def test_numbers_are_compared_numerically_not_as_text(self):
        self.assertEqual(ic.compare_versions('2.10.0', '2.9.9'), 1)
        self.assertEqual(ic.compare_versions('2.4.0', '2.5.0'), -1)
        self.assertEqual(ic.compare_versions('v2.5.0', '2.5.0'), 0)
        self.assertEqual(ic.compare_versions('2.5', '2.5.0'), 0)

    def test_a_release_is_newer_than_its_own_pre_releases(self):
        self.assertEqual(ic.compare_versions('2.5.0', '2.5.0-beta.1'), 1)
        self.assertEqual(ic.compare_versions('2.5.0-beta.2', '2.5.0-beta.1'), 1)

    def test_something_unparsable_never_counts_as_newer(self):
        self.assertEqual(ic.compare_versions('legacy', '2.5.0'), -1)
        self.assertEqual(ic.compare_versions('2.5.0', 'nonsense'), 1)


class FlowBase(InstallBase):
    def bundle(self, lock, damage=None):
        """An installer file whose non-bootstrap entries are named exactly like the lock's components."""
        routes = self.server.routes
        names = {'python': 'py.zip', 'yt-dlp': 'yt-dlp.exe', 'deno': 'deno.zip', 'ffmpeg': 'ffmpeg.zip'}
        entries = []
        for entry in lock['components']:
            data = bytearray(routes['/' + names[entry['name']]])
            if damage == entry['name']:
                data[len(data) // 2] ^= 0xFF
            entries.append((entry['name'], bytes(data), False))
        path = self.tmp / 'BraveMusicDownloader-Setup-Offline.exe'
        path.write_bytes(pc.build(fake_pe(), entries))
        return {'path': str(path), 'entries': pc.by_name(pc.read(path))}


class DowngradeGuardTests(FlowBase):
    def test_an_older_installer_refuses_to_downgrade_and_changes_nothing(self):
        self.install()
        self.server.hits.clear()
        manifest = json.loads(self.layout.manifest.read_text())
        manifest['version'] = '9.9.9'
        self.layout.manifest.write_text(json.dumps(manifest))
        before = self.snapshot()
        with self.assertRaises(ic.DowngradeRefused) as caught:
            self.install()
        self.assertIn('9.9.9 is already installed', str(caught.exception))
        self.assertIn('AllowDowngrade', str(caught.exception))
        self.assertEqual(caught.exception.category, 'downgrade')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.server.hits, [], 'nothing was downloaded')

    def test_the_same_version_is_a_repair_not_a_downgrade(self):
        self.install()
        (self.layout.app / 'tools' / 'ffmpeg.exe').unlink()
        self.server.hits.clear()
        manifest = self.install()
        self.assertEqual(manifest['update']['repaired'], ['ffmpeg'])
        self.assertIn('/ffmpeg.zip', self.server.hits)

    def test_downgrade_is_possible_only_when_explicitly_allowed(self):
        self.install()
        manifest = json.loads(self.layout.manifest.read_text())
        manifest['version'] = '9.9.9'
        self.layout.manifest.write_text(json.dumps(manifest))
        done = self.install(allow_downgrade=True)
        self.assertEqual(done['version'], ic.VERSION)
        self.assertEqual(done['update']['previous_version'], '9.9.9')

    def test_a_legacy_install_has_no_version_to_compare(self):
        self.make_old_app(version_note='legacy')
        bmdfs.write_json_atomic(self.layout.manifest, {'product': 'BraveMusicDownloader', 'schema': 1,
                                                        'version': 'legacy', 'components': []})
        self.install()
        self.assertEqual(json.loads(self.layout.manifest.read_text())['update']['previous_version'], 'legacy')


class BundleTests(FlowBase):
    def setUp(self):
        super().setUp()
        pinned(self.lock, self.server.routes)

    def test_an_offline_installer_needs_no_network_for_tools(self):
        self.server.hits.clear()
        manifest = self.install(bundle=self.bundle(self.lock))
        self.assertEqual(self.server.hits, [])
        sources = {c['name']: c['source'] for c in manifest['components']}
        self.assertEqual(sources, {'python': 'bundle', 'yt-dlp': 'bundle', 'deno': 'bundle', 'ffmpeg': 'bundle'})
        for comp in manifest['components']:
            self.assertEqual(comp['verified_by'], 'pinned')
        self.assertTrue((self.layout.app / 'tools' / 'ffmpeg.exe').is_file())

    def test_each_bundled_file_is_verified_against_the_pinned_hash(self):
        with self.assertRaises(ic.InstallerDamaged) as caught:
            self.install(bundle=self.bundle(self.lock, damage='ffmpeg'))
        self.assertIn('does not match the version pinned', str(caught.exception))
        self.assertIn('damaged', str(caught.exception))
        self.assertEqual(caught.exception.category, 'damaged')
        self.assertFalse(self.layout.app.exists(), 'a failed fresh install leaves no app folder')
        self.assertEqual(self.registry.keys, {})

    def test_a_damaged_bundle_never_touches_the_previous_installation(self):
        self.install()
        (self.layout.app / 'tools' / 'yt-dlp.exe').unlink()           # forces yt-dlp to be supplied again
        before = self.snapshot()
        self.server.httpd.shutdown()                      # no downloads possible at all
        with self.assertRaises(ic.InstallerDamaged):
            self.install(bundle=self.bundle(self.lock, damage='yt-dlp'))
        self.assertEqual(self.snapshot(), before)

    def test_a_verified_copy_already_in_the_cache_is_reused(self):
        bundle = self.bundle(self.lock)
        self.install(bundle=bundle, keep_downloads=True)
        archives = {p.name: p.stat().st_mtime_ns for p in self.layout.downloads.iterdir()}
        self.assertTrue(archives)
        (self.layout.app / 'tools' / 'ffmpeg.exe').unlink()             # forces ffmpeg to be supplied again
        again = self.install(bundle=bundle, keep_downloads=True)
        repaired = {c['name']: c for c in again['components']}
        self.assertEqual(repaired['ffmpeg']['source'], 'bundle')
        self.assertEqual({p.name: p.stat().st_mtime_ns for p in self.layout.downloads.iterdir()}, archives,
                         'the verified copy in the cache was used, not a new one from the installer file')


class ProgramsAndFeaturesTests(FlowBase):
    def test_install_creates_the_entry_and_names_this_installations_uninstaller(self):
        self.install()
        entry = self.registry.values[ic.ARP_KEY]
        self.assertEqual(entry['DisplayName'], 'Brave Music Downloader')
        self.assertEqual(entry['DisplayVersion'], ic.VERSION)
        self.assertEqual(Path(entry['InstallLocation']), self.layout.root)
        self.assertIn(str(self.layout.app / 'Uninstall.ps1'), entry['UninstallString'])
        self.assertTrue(entry['QuietUninstallString'].endswith('-Silent'))
        self.assertGreater(entry['EstimatedSize'], 0)

    def test_a_failed_fresh_install_leaves_no_entry(self):
        with self.assertRaises(ic.DownloadFailed):
            self.install(opener=self.refusing)
        self.assertEqual(self.registry.values, {})

    refusing = staticmethod(lambda request, timeout=60: (_ for _ in ()).throw(ic.DownloadFailed('offline')))

    def test_a_failed_upgrade_restores_the_previous_entry(self):
        self.install()
        self.registry.values[ic.ARP_KEY]['DisplayVersion'] = '1.0.0'
        (self.layout.app / 'tools' / 'ffmpeg.exe').unlink()
        with self.assertRaises(ic.DownloadFailed):
            self.install(opener=self.refusing)
        self.assertEqual(self.registry.values[ic.ARP_KEY]['DisplayVersion'], '1.0.0')

    def test_uninstall_removes_the_entry(self):
        self.install()
        ic.uninstall(self.layout, self.registry)
        self.assertNotIn(ic.ARP_KEY, self.registry.values)

    def test_an_entry_belonging_to_another_installation_is_left_alone(self):
        self.registry.set_values(ic.ARP_KEY, {'DisplayName': 'Brave Music Downloader',
                                              'InstallLocation': str(self.tmp / 'elsewhere')})
        report = ic.uninstall(self.layout, self.registry)
        self.assertEqual(report['failed'], [])
        self.assertIn(ic.ARP_KEY, self.registry.values)
        self.assertTrue(any('Programs and Features' in s for s in report['skipped']))


class StartMenuTests(FlowBase):
    def test_shortcuts_are_removed_but_neighbours_survive(self):
        roaming = self.tmp / 'Roaming'
        with mock.patch.dict(os.environ, {'APPDATA': str(roaming)}):
            layout = ic.Layout(self.layout.root, self.layout.data_dir, self.layout.output)
            self.assertEqual(layout.start_menu, roaming / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs'
                             / 'Brave Music Downloader')
            other = layout.start_menu.parent / 'Another App'
            other.mkdir(parents=True)
            (other / 'x.lnk').write_text('x')
            layout.start_menu.mkdir()
            (layout.start_menu / 'Dashboard.lnk').write_text('x')
            report = ic.uninstall(layout, self.registry)
            self.assertFalse(layout.start_menu.exists())
            self.assertTrue((other / 'x.lnk').exists())
            self.assertTrue(any('Start Menu shortcuts' in s for s in report['removed']))

    def test_without_a_start_menu_location_nothing_is_assumed(self):
        with mock.patch.dict(os.environ, {'APPDATA': ''}):
            layout = ic.Layout(self.layout.root, self.layout.data_dir, self.layout.output)
            self.assertIsNone(layout.start_menu)
            report = ic.uninstall(layout, self.registry)
            self.assertEqual(report['failed'], [])


class ProgressAndCancelTests(FlowBase):
    def events(self, path):
        return [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines()]

    def test_the_engine_reports_phases_components_and_download_bytes(self):
        progress = self.tmp / 'progress.jsonl'
        self.install(control=ic.Control(progress, throttle=0))
        events = self.events(progress)
        phases = [e['phase'] for e in events if e['event'] == 'phase']
        self.assertEqual(phases[0], 'preparing')
        self.assertEqual(phases[-1], 'installed')
        self.assertIn('swapping', phases)
        components = [e['name'] for e in events if e['event'] == 'component']
        self.assertEqual(components, ['python', 'yt-dlp', 'deno', 'ffmpeg'])
        downloads = [e for e in events if e['event'] == 'download']
        self.assertEqual(len(downloads), 4, 'one byte-progress line per component')
        done = {e['url']: e['bytes'] for e in events if e['event'] == 'downloaded'}
        self.assertEqual(done[self.server.base + '/py.zip'], len(self.server.routes['/py.zip']))
        self.assertEqual(done[self.server.base + '/ffmpeg.zip'], len(self.server.routes['/ffmpeg.zip']))
        for line in Path(progress).read_text(encoding='utf-8').splitlines():     # one JSON object per line
            json.loads(line)

    def test_progress_is_throttled_by_default(self):
        progress = self.tmp / 'progress.jsonl'
        self.install(control=ic.Control(progress))
        self.assertEqual([e for e in self.events(progress) if e['event'] == 'download'], [],
                         'a download that finishes within the throttle interval reports only its completion')

    def test_without_a_progress_file_nothing_is_written(self):
        before = set(self.tmp.rglob('*'))
        self.install(control=ic.Control(None, None))
        new = set(self.tmp.rglob('*')) - before
        self.assertTrue(new and all(p == self.layout.root.parent or self.layout.root.parent in p.parents
                                    for p in new), 'only the install folder appeared')

    def test_cancel_before_the_swap_restores_the_previous_installation(self):
        self.install()
        before = self.snapshot()
        (self.layout.app / 'tools' / 'deno.exe').unlink()
        cancel = self.tmp / 'cancel.flag'
        with self.assertRaises(ic.SetupCancelled) as caught:
            self.install(control=ic.Control(None, cancel), hooks={'staged': cancel.touch})
        self.assertEqual(caught.exception.category, 'cancelled')
        self.assertEqual(json.loads(self.layout.manifest.read_text())['version'], ic.VERSION)
        self.assertFalse(self.layout.journal.exists())
        self.assertEqual(self.leftovers(), [])

    def test_cancel_while_downloading_stops_at_once_and_leaves_nothing_on_a_fresh_machine(self):
        cancel = self.tmp / 'cancel.flag'

        def opener(request, timeout=60):
            cancel.touch()
            return ic.default_opener(request, timeout)

        with self.assertRaises(ic.SetupCancelled):
            self.install(control=ic.Control(None, cancel), opener=opener)
        self.assertFalse(self.layout.app.exists())
        self.assertEqual(self.leftovers(), [])
        self.assertFalse(list(self.layout.downloads.glob('*.partial')))

    def test_cancel_requested_during_the_critical_phase_is_ignored(self):
        cancel = self.tmp / 'cancel.flag'
        manifest = self.install(control=ic.Control(None, cancel), hooks={'between_renames': cancel.touch})
        self.assertEqual(manifest['version'], ic.VERSION)
        self.assertTrue(cancel.exists())


class ResultFileTests(FlowBase):
    def run_main(self, *extra, payload=None):
        """Run the engine CLI; the lock comes from a file, or from `payload` when one is given."""
        result = self.tmp / 'result.json'
        lock = self.tmp / 'tools.lock.json'
        lock.write_text(json.dumps(self.lock), encoding='utf-8')
        lock_args = [] if payload else ['--lock', str(lock)]
        code = ic.main(['install', '--payload', str(payload or self.payload), *lock_args,
                        '--root', str(self.layout.root),
                        '--data-dir', str(self.layout.data_dir), '--output', str(self.layout.output),
                        '--extension-id', EXT_ID, '--registry', 'fake', '--result-file', str(result), *extra])
        return code, json.loads(result.read_text(encoding='utf-8'))

    def test_success_reports_the_update_summary(self):
        code, outcome = self.run_main()
        self.assertEqual(code, 0)
        self.assertTrue(outcome['ok'])
        self.assertIsNone(outcome['category'])
        self.assertEqual(outcome['result']['version'], ic.VERSION)
        self.assertTrue(outcome['result']['fresh_install'])
        self.assertEqual([c['name'] for c in outcome['result']['components']],
                         ['python', 'yt-dlp', 'deno', 'ffmpeg'])

    def test_failures_carry_their_category_and_the_engine_exit_code(self):
        code, outcome = self.run_main(payload=self.bad_checksum_payload())
        self.assertEqual((code, outcome['ok'], outcome['category']), (2, False, 'download'))
        self.assertIn('Checksum mismatch', outcome['message'])

    def bad_checksum_payload(self):
        lock = json.loads(json.dumps(self.lock))
        lock['components'][3]['sha256'] = '0' * 64            # ffmpeg
        path = self.tmp / 'payload-bad.zip'
        with zipfile.ZipFile(self.payload) as source, zipfile.ZipFile(path, 'w') as target:
            for name in source.namelist():
                target.writestr(name, json.dumps(lock) if name == 'tools.lock.json' else source.read(name))
        return path

    def test_an_unexpected_error_is_reported_as_failed_not_as_success(self):
        result = self.tmp / 'result.json'
        with mock.patch.object(ic, 'install', side_effect=RuntimeError('boom')), contextlib.redirect_stdout(io.StringIO()):
            code = ic.main(['install', '--payload', str(self.payload), '--root', str(self.layout.root),
                            '--extension-id', EXT_ID, '--registry', 'fake', '--result-file', str(result)])
        outcome = json.loads(result.read_text(encoding='utf-8'))
        self.assertEqual((code, outcome['category']), (1, 'failed'))
        self.assertIn('RuntimeError', outcome['message'])

    def test_a_damaged_installer_file_is_category_damaged(self):
        damaged = self.tmp / 'damaged.exe'
        damaged.write_bytes(b'not an installer' * 10)
        with contextlib.redirect_stdout(io.StringIO()):
            code = self.run_main('--bundle', str(damaged))[0]
        self.assertEqual(code, 2)
        self.assertEqual(json.loads((self.tmp / 'result.json').read_text())['category'], 'damaged')

    def test_a_second_setup_is_reported_as_busy(self):
        self.layout.root.mkdir(parents=True)
        self.layout.lockfile.write_text(str(os.getpid()))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = self.run_main()[0]
        self.assertEqual(code, 2)
        self.assertEqual(json.loads((self.tmp / 'result.json').read_text())['category'], 'busy')
        self.assertIn('already running', out.getvalue())


class EngineCliContractTests(FlowBase):
    """What Setup.ps1 relies on when it starts the engine (tests/ps/Test-Setup.ps1 is the other half)."""

    def run_engine(self, *arguments):
        result = self.tmp / 'result.json'
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = ic.main([*arguments, '--root', str(self.layout.root), '--registry', 'fake',
                            '--result-file', str(result)])
        return code, out.getvalue(), result

    def test_the_log_file_receives_a_copy_of_every_line_the_engine_prints(self):
        log = self.tmp / 'engine.log'
        code, printed, result = self.run_engine('diagnose', '--log-file', str(log))
        lines = [line for line in printed.splitlines() if line]
        self.assertTrue(lines, 'diagnose reports its checks')
        self.assertEqual([line for line in log.read_text(encoding='utf-8').splitlines() if line], lines)
        self.assertIn(f'Diagnosing Brave Music Downloader {ic.VERSION}', printed)
        self.assertTrue(result.is_file())
        log.write_text('earlier entry\n', encoding='utf-8')                 # appended to, never replaced
        self.run_engine('verify', '--log-file', str(log))
        self.assertTrue(log.read_text(encoding='utf-8').startswith('earlier entry\n'))

    def test_an_unwritable_log_file_never_stops_the_engine(self):
        code, printed, result = self.run_engine('diagnose', '--log-file', str(self.tmp / 'no' / 'such' / 'dir' / 'x.log'))
        self.assertIn(code, (0, 1, 2))
        self.assertIn('Diagnosing', printed)
        self.assertTrue(result.is_file())

    def test_every_command_takes_the_channel_files_setup_always_passes(self):
        for command in ('diagnose', 'uninstall', 'verify', 'hostcheck'):
            code, _, result = self.run_engine(command, '--progress-file', str(self.tmp / f'{command}.jsonl'),
                                              '--cancel-file', str(self.tmp / f'{command}.cancel'),
                                              '--log-file', str(self.tmp / f'{command}.log'))
            self.assertIn(code, (0, 1, 2), command)       # an unknown option would end the program (SystemExit)
            self.assertTrue(result.is_file(), command)
            self.assertFalse((self.tmp / f'{command}.jsonl').exists(), 'only install reports progress')

    def test_no_program_is_started_without_the_no_window_flag(self):
        """Setup runs without a window (GUI, silent); a console program started without the flag would
        open a console window of its own. Guarded here so a new call cannot forget it."""
        source = (ROOT / 'app' / 'installer' / 'installer_core.py').read_text(encoding='utf-8')
        starts = [m.start() for m in re.finditer(r'subprocess\.(run|Popen|check_output|call|check_call)\(', source)]
        self.assertGreaterEqual(len(starts), 7)
        for number, at in enumerate(starts):
            end = starts[number + 1] if number + 1 < len(starts) else len(source)
            self.assertIn('creationflags=NO_WINDOW', source[at:end], source[at:at + 60])

    def test_the_flag_is_the_windows_one_and_harmless_elsewhere(self):
        self.assertEqual(ic.NO_WINDOW, getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        calls = []

        def record(command, **kwargs):
            calls.append(kwargs.get('creationflags'))
            output = kwargs.get('env', {}).get('BMD_FETCH_OUT')
            if output:
                Path(output).write_bytes(b'downloaded')
            return subprocess.CompletedProcess(command, 0, 'ok', '')

        staging = self.tmp / 'staging'
        (staging / 'helper').mkdir(parents=True)
        csc = self.tmp / 'Windows' / 'Microsoft.NET' / 'Framework64' / 'v4.0.30319' / 'csc.exe'
        csc.parent.mkdir(parents=True)
        csc.touch()
        launcher = self.tmp / 'NativeLauncher.exe'
        launcher.touch()
        request = urllib.request.Request('https://example.invalid/tool.zip')
        with mock.patch.object(ic.subprocess, 'run', record), mock.patch.object(ic, 'on_windows', return_value=True), \
                mock.patch.dict(os.environ, WINDIR=str(self.tmp / 'Windows')), contextlib.redirect_stdout(io.StringIO()):
            ic.smoke_test(staging)
            ic.build_launcher_windows(staging / 'NativeLauncher.cs', launcher)
            ic.stop_processes_under(staging)
            ic.powershell_open(request)
        self.assertEqual(len(calls), 9)
        self.assertEqual(calls, [ic.NO_WINDOW] * 9)


class DiagnoseTests(FlowBase):
    def test_a_report_lists_every_check_with_a_verdict(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            report = ic.diagnose(self.layout, self.registry, {'installed': True, 'version': '1.2.3.4'})
        names = [c['check'] for c in report['checks']]
        self.assertIn('installation record', names)
        self.assertIn('yt-dlp is installed', names)
        self.assertIn('Brave native messaging registration', names)
        self.assertIn('extension has contacted the helper', names)
        self.assertIn('free disk space', names)
        self.assertFalse(report['ok'])
        self.assertEqual(report['browser'], {'installed': True, 'version': '1.2.3.4'})
        self.assertIn('FAIL', out.getvalue())
        self.assertTrue(all(set(c) == {'check', 'ok', 'detail'} for c in report['checks']))

    def test_an_installed_system_passes_its_registration_checks(self):
        self.install()
        self.layout.launcher.touch()                 # the real installer compiles it (builder)
        with contextlib.redirect_stdout(io.StringIO()):
            report = ic.diagnose(self.layout, self.registry)
        verdicts = {c['check']: c['ok'] for c in report['checks']}
        self.assertTrue(verdicts['installation record'])
        self.assertTrue(verdicts['Chrome native messaging registration'])
        self.assertTrue(verdicts['Brave native messaging registration'])
        self.assertTrue(verdicts['native host manifest allows the extension'])
        self.assertTrue(verdicts['native messaging launcher exists'])
        self.assertTrue(verdicts['settings and history folder is writable'])

    def test_verify_names_a_broken_registration(self):
        self.install()
        self.layout.launcher.touch()
        self.assertTrue(ic.verify(self.layout, self.registry)['ok'])
        self.registry.delete(ic.BROWSER_KEYS['Brave'])
        report = ic.verify(self.layout, self.registry)
        self.assertFalse(report['ok'])
        self.assertTrue(any('Brave' in p for p in report['problems']))


class ExtensionContactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_without_a_marker_the_extension_counts_as_not_connected(self):
        self.assertEqual(ic.extension_contact(self.tmp),
                         {'seen': False, 'fresh': False, 'age_seconds': None, 'version': None, 'method': None})

    def test_a_recent_contact_is_fresh_and_an_old_one_is_not(self):
        now = 1_800_000_000.0
        bmdfs.write_json_atomic(self.tmp / 'extension-contact.json', {'time': now - 20, 'version': '2.5.0'})
        self.assertTrue(ic.extension_contact(self.tmp, now)['fresh'])
        old = ic.extension_contact(self.tmp, now + 3600)
        self.assertEqual((old['seen'], old['fresh'], old['age_seconds']), (True, False, 3620.0))
        bmdfs.write_json_atomic(self.tmp / 'extension-contact.json', {'time': 'yesterday'})
        self.assertFalse(ic.extension_contact(self.tmp, now)['seen'])

    def test_the_helper_records_browser_traffic_at_most_every_fifteen_seconds(self):
        data = self.tmp / 'data'
        clock = {'now': 1000.0}
        marker = data / 'extension-contact.json'
        with mock.patch.dict(os.environ, {'BMD_DATA_DIR': str(data)}), mock.patch.object(paths, 'DATA_DIR', data), \
                mock.patch.object(paths, 'CONTACT_FILE', marker), \
                mock.patch.object(native_host.time, 'time', lambda: clock['now']):
            native_host._CONTACT_AT[0] = 0.0
            native_host.mark_contact('health')
            self.assertEqual(json.loads(marker.read_text()), {'time': 1000.0, 'version': ic.VERSION, 'method': 'health'})
            clock['now'] = 1005.0
            native_host.mark_contact('jobs')                        # 5 s later: throttled
            self.assertEqual(json.loads(marker.read_text())['method'], 'health')
            clock['now'] = 1020.0
            native_host.mark_contact('jobs')                        # 20 s: written
        self.assertEqual(json.loads(marker.read_text()), {'time': 1020.0, 'version': ic.VERSION, 'method': 'jobs'})

    def test_setup_health_checks_do_not_count_as_extension_contact(self):
        data = self.tmp / 'data'
        with mock.patch.dict(os.environ, {'BMD_NO_CONTACT_MARK': '1'}), \
                mock.patch.object(paths, 'DATA_DIR', data), \
                mock.patch.object(paths, 'CONTACT_FILE', data / 'extension-contact.json'):
            native_host._CONTACT_AT[0] = 0.0
            native_host.mark_contact('health')
        self.assertFalse((data / 'extension-contact.json').exists())

    def test_round_trips_started_by_setup_tell_the_helper_not_to_record_them(self):
        marker = self.tmp / 'env.txt'
        host = script(self.tmp, 'host.py', REPLY_PRELUDE + """
import os
from pathlib import Path
Path(os.environ['MARK_OUT']).write_text(os.environ.get('BMD_NO_CONTACT_MARK', ''))
request = read_request()
reply({'id': request['id'], 'ok': True, 'data': {'version': %r}})
""" % ic.VERSION)
        ic.native_roundtrip(host, env=dict(os.environ, MARK_OUT=str(marker)))
        self.assertEqual(marker.read_text(), '1')

    def test_the_real_helper_writes_the_marker_for_a_browser_request_only(self):
        host = ROOT / 'app' / 'helper' / 'native_host.py'
        for flagged, expected in ((False, True), (True, False)):
            data = self.tmp / f'data-{flagged}'
            env = dict(os.environ, BMD_INSTALL_ROOT=str(self.tmp / f'root-{flagged}'), BMD_DATA_DIR=str(data),
                       BMD_OUTPUT=str(self.tmp / f'music-{flagged}'), PYTHONDONTWRITEBYTECODE='1')
            env.pop('BMD_NO_CONTACT_MARK', None)
            if flagged:
                env['BMD_NO_CONTACT_MARK'] = '1'
            raw = json.dumps({'id': 7, 'method': 'health'}).encode()
            run = subprocess.run([sys.executable, str(host)], input=struct.pack('<I', len(raw)) + raw,
                                 capture_output=True, env=env, timeout=60)
            self.assertEqual(run.returncode, 0, run.stderr.decode())
            marker = data / 'extension-contact.json'
            self.assertEqual(marker.exists(), expected)
            if expected:
                record = json.loads(marker.read_text())
                self.assertEqual((record['method'], record['version']), ('health', ic.VERSION))
                self.assertEqual(set(record), {'time', 'version', 'method'}, 'no browsing data is recorded')

    def test_uninstall_removes_the_marker_with_the_other_runtime_files(self):
        layout = ic.Layout(self.tmp / 'BMD', self.tmp / 'data', self.tmp / 'music')
        layout.data_dir.mkdir(parents=True)
        (layout.data_dir / 'extension-contact.json').write_text('{}')
        (layout.data_dir / 'native-host.lock').write_text('0')
        report = ic.uninstall(layout, ic.FakeRegistry())
        self.assertFalse((layout.data_dir / 'extension-contact.json').exists())
        self.assertTrue(any('extension-contact.json' in s for s in report['removed']))


if __name__ == '__main__':
    unittest.main()
