"""The installation manager's assessment (installer_core.manage_status).

These tests run the real assessment against real (fake-payload) installs in a temporary
layout, with `probe=False` so no component is executed and no helper is started: the table
logic - states, ownership, update comparison, legacy recognition, interruption - is what is
under test here. The probe (tool version runs and the helper round trip) is exercised on a
real installation by the PowerShell harness and by Setup.exe /REPAIR's own flow.
"""
import unittest

from installer_support import EXT_ID, Server, fake_components, sha  # noqa: F401
from support import tmpdir

import bmdfs
import installer_core as ic


def build_install(self):
    """A healthy 2.5.0 install in a temporary layout; returns (layout, registry)."""
    server = Server()
    try:
        layout = ic.Layout(self.tmp / 'e2e' / 'Local' / 'BMD', self.tmp / 'e2e' / 'Roaming' / 'BMD',
                           self.tmp / 'e2e' / 'Music')
        registry = ic.FakeRegistry()
        ic.install(layout, self.payload, fake_components(server), registry,
                   extension_id=EXT_ID, rename_retries=1)
        return layout, registry
    finally:
        server.close()


def fresh_contact(layout):
    import json
    import time
    from pathlib import Path
    path = Path(layout.data_dir) / 'extension-contact.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'time': time.time(), 'version': ic.VERSION, 'method': 'jobs'}), encoding='utf-8')


def state_of(report, cid):
    return next(c for c in report['components'] if c['id'] == cid)


class ManageStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.out = self.tmp / 'dist'
        self.out.mkdir()
        server = Server()
        try:
            from installer_support import fake_components as _fc
            from pathlib import Path
            import zipfile
            import io
            files = {'extension/manifest.json': (Path(__file__).resolve().parents[1] / 'app' / 'extension' / 'manifest.json').read_text(encoding='utf-8'),
                     'helper/native_host.py': '#host', 'helper/helper.py': '#h', 'helper/bmdfs.py': '#fs',
                     'bridge/NativeLauncher.cs': '// launcher', 'installer/installer_core.py': '#core',
                     'installer/payload_container.py': '#container', 'README.md': 'readme',
                     'Uninstall.ps1': '# un', 'tools.lock.json': '{}'}
            payload = self.out / 'Application.zip'
            with zipfile.ZipFile(payload, 'w') as z:
                for name, data in files.items():
                    z.writestr(name, data)
            self.payload = payload
        finally:
            server.close()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_fresh_machine_reports_not_installed(self):
        layout = ic.Layout(self.tmp / 'Local' / 'BMD', self.tmp / 'Roaming' / 'BMD', self.tmp / 'Music')
        report = ic.manage_status(layout, ic.FakeRegistry(), probe=False)
        self.assertEqual(report['overall'], 'not-installed')
        self.assertEqual(state_of(report, 'record')['state'], 'missing')
        self.assertEqual(state_of(report, 'brave')['state'], 'missing')
        self.assertEqual(state_of(report, 'extension')['state'], 'na')
        self.assertTrue(report['headline'].startswith('Brave Music Downloader is not installed'))
        self.assertTrue(any('Recommended action: Install.' in line for line in ic.manage_status_lines(report)))

    def test_a_healthy_install_reports_ok_with_every_component_green(self):
        layout, registry = build_install(self)
        (layout.app / 'bridge' / 'BraveMusicNativeHost.exe').write_bytes(b'MZ fake launcher')
        fresh_contact(layout)
        report = ic.manage_status(layout, registry, probe=False)
        self.assertEqual(report['overall'], 'ok')
        for cid in ('record', 'app-files', 'python', 'yt-dlp', 'deno', 'ffmpeg', 'native-messaging', 'extension'):
            self.assertEqual(state_of(report, cid)['state'], 'ok', cid)
        self.assertTrue(report['headline'].startswith(f'Brave Music Downloader {ic.VERSION} is installed and working'))
        self.assertTrue(any('Recommended action: none' in line for line in ic.manage_status_lines(report)))

    def test_an_older_setup_offers_an_update_and_a_newer_one_is_not_downgraded(self):
        layout, registry = build_install(self)
        (layout.app / 'bridge' / 'BraveMusicNativeHost.exe').write_bytes(b'MZ')
        manifest = bmdfs.read_json(layout.manifest)
        manifest['version'] = '2.4.0'
        bmdfs.write_json_atomic(layout.manifest, manifest)
        report = ic.manage_status(layout, registry, probe=False)
        self.assertEqual(report['overall'], 'update')
        self.assertTrue(report['update_available'])
        self.assertIn('update available', state_of(report, 'update')['detail'])
        manifest['version'] = '9.9.9'
        bmdfs.write_json_atomic(layout.manifest, manifest)
        report = ic.manage_status(layout, registry, probe=False)
        self.assertEqual(report['overall'], 'ok', 'a newer install is left alone')
        self.assertIn('NEWER', state_of(report, 'update')['detail'])

    def test_a_missing_tool_is_reported_and_repair_is_recommended(self):
        layout, registry = build_install(self)
        (layout.app / 'bridge' / 'BraveMusicNativeHost.exe').write_bytes(b'MZ')
        (layout.app / 'tools' / 'yt-dlp.exe').unlink()
        report = ic.manage_status(layout, registry, probe=False)
        self.assertEqual(report['overall'], 'repair')
        self.assertEqual(state_of(report, 'yt-dlp')['state'], 'missing')
        self.assertTrue(any('Recommended action: Repair.' in line for line in ic.manage_status_lines(report)))

    def test_broken_native_messaging_names_the_problem(self):
        layout, registry = build_install(self)
        (layout.app / 'bridge' / 'BraveMusicNativeHost.exe').write_bytes(b'MZ')
        registry.set(ic.BROWSER_KEYS['Brave'], r'C:\somewhere\else.json')
        report = ic.manage_status(layout, registry, probe=False)
        self.assertEqual(report['overall'], 'repair')
        self.assertIn('points elsewhere', state_of(report, 'native-messaging')['detail'])

    def test_an_unknown_app_folder_is_reported_but_never_touched(self):
        layout = ic.Layout(self.tmp / 'Local' / 'BMD', self.tmp / 'Roaming' / 'BMD', self.tmp / 'Music')
        (layout.app / 'something').mkdir(parents=True)
        report = ic.manage_status(layout, ic.FakeRegistry(), probe=False)
        self.assertEqual(report['overall'], 'not-installed', 'an unrecognized folder is not an installation')
        self.assertEqual(state_of(report, 'app-files')['state'], 'broken')
        self.assertIn('not touched', state_of(report, 'app-files')['detail'])

    def test_a_legacy_install_is_recognized_conservatively(self):
        layout, registry = build_install(self)
        (layout.app / 'bridge' / 'BraveMusicNativeHost.exe').write_bytes(b'MZ')
        layout.manifest.unlink()                       # a pre-2.3 install has no ownership record
        report = ic.manage_status(layout, registry, probe=False)
        self.assertTrue(report['legacy'])
        self.assertIn('recognized', state_of(report, 'record')['detail'])
        self.assertEqual(report['overall'], 'ok')

    def test_an_interrupted_setup_recommends_repair(self):
        layout, registry = build_install(self)
        (layout.app / 'bridge' / 'BraveMusicNativeHost.exe').write_bytes(b'MZ')
        bmdfs.write_json_atomic(layout.journal, {'id': 'abc', 'state': 'staging'})
        report = ic.manage_status(layout, registry, probe=False)
        self.assertEqual(report['overall'], 'repair')
        self.assertTrue(report['interrupted'])
        self.assertIn('interrupted', state_of(report, 'journal')['detail'])

    def test_the_extension_line_follows_the_contact_marker(self):
        layout, registry = build_install(self)
        (layout.app / 'bridge' / 'BraveMusicNativeHost.exe').write_bytes(b'MZ')
        report = ic.manage_status(layout, registry, probe=False)
        self.assertEqual(state_of(report, 'extension')['state'], 'warn')
        self.assertIn('never contacted', state_of(report, 'extension')['detail'])
        fresh_contact(layout)
        report = ic.manage_status(layout, registry, probe=False)
        self.assertEqual(state_of(report, 'extension')['state'], 'ok')


if __name__ == '__main__':
    unittest.main()
