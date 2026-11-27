"""Run tests/ps/Test-Setup.ps1, the tests of Setup.ps1 and app/Uninstall.ps1's logic, through PowerShell.

The harness dot-sources both scripts and calls their functions with files in a temporary folder, a
stand-in engine and a local HTTP server; the entry points run in child PowerShell processes. Cases
that need an interpreter, a file served over HTTP or a real Application.zip skip unless they are
provided in the environment, so this module builds the real application archive from a copy of the
tree (the release gates run first) and serves one file whose SHA-256 is computed here, in Python,
as an independent check of what PowerShell downloads and hashes.

Every harness case becomes one unittest: it passes, fails with the harness's reason, or skips.
Needs PowerShell 7 (`pwsh` on the PATH, or BMD_TEST_PWSH=<file>); without it the module is skipped.
What this cannot show is listed at the top of the harness (the window, the real registry, a real
Brave, code signatures): those steps are in WINDOWS_QA.md.
"""
import os
import shutil
import subprocess
import sys
import unittest

from installer_support import Server, sha
from support import ROOT, tmpdir

import test_release as tr

sys.path.insert(0, str(ROOT / 'scripts'))
import build_release as br  # noqa: E402

HARNESS = ROOT / 'tests' / 'ps' / 'Test-Setup.ps1'
# The skips that remain on Windows when everything the harness can use is provided: these two
# cases need a machine without Brave, and one without a desktop. Elsewhere nothing may skip.
EXPECTED_SKIPS = {'nt': {'entry-require-brave', 'entry-no-window'}}


def powershell():
    return os.environ.get('BMD_TEST_PWSH') or shutil.which('pwsh')


def run_harness(pwsh):
    """(exit code, output, error text) of one run of the harness with everything it can use."""
    folder = tmpdir()
    server = None
    try:
        source = tr.copy_source(folder / 'source')
        br.check_versions(source)
        br.check_identity(source)
        br.check_extension(source)
        br.check_lock(source, False)
        entries = br.collect(source)
        br.check_content(entries, source)
        payload = folder / 'Application.zip'
        br.build_zip(entries, payload)
        # The harness reads the served file from disk through the URL's path, so the URL carries it.
        served = folder / 'served' / 'python.zip'
        served.parent.mkdir()
        served.write_bytes(b'PK\x03\x04' + os.urandom(64) * 300)
        route = '/' + str(served).lstrip('/')
        server = Server({route: served.read_bytes()})
        environment = dict(os.environ, BMD_TEST_PYTHON=sys.executable, BMD_TEST_PAYLOAD=str(payload),
                           BMD_TEST_URL=server.base + route, BMD_TEST_SHA256=sha(served.read_bytes()),
                           BMD_TEST_TMP=str(folder / 'scratch'))
        environment.pop('TMPDIR', None)
        if os.name != 'nt':
            environment.setdefault('DOTNET_SYSTEM_GLOBALIZATION_INVARIANT', '1')   # a pwsh without ICU libraries
        run = subprocess.run([pwsh, '-NoLogo', '-NoProfile', '-File', str(HARNESS), '-Repo', str(source)],
                             capture_output=True, text=True, timeout=3600, env=environment,
                             stdin=subprocess.DEVNULL)
        return run.returncode, run.stdout, run.stderr
    finally:
        if server:
            server.close()
        shutil.rmtree(folder, ignore_errors=True)


class SetupHarnessTests(unittest.TestCase):
    """One test per case of the harness, plus two about the run as a whole."""
    results = {}                   # case name -> (status, reason)
    outcome = None                 # (exit code, error text, the harness's own "<cases>|<failed>" line)
    host = None                    # what the harness reports about its PowerShell and system
    missing = None                 # why the harness cannot run here: every test skips

    @classmethod
    def prepare(cls):
        """Run the harness once; a second call returns at once."""
        if cls.outcome is not None or cls.missing:
            return
        pwsh = powershell()
        if not pwsh:
            cls.missing = 'needs PowerShell 7 (pwsh on the PATH, or BMD_TEST_PWSH=<file>)'
            return
        code, out, err = run_harness(pwsh)
        results, summary = {}, None
        for line in out.splitlines():
            kind, _, rest = line.partition('|')
            if kind == 'host':
                cls.host = rest
            elif kind == 'cases':
                summary = rest
            elif kind in ('pass', 'fail', 'skip'):
                name, _, reason = rest.partition('|')
                results[name] = (kind, reason)
        cls.results, cls.outcome = results, (code, err.strip(), summary)

    def setUp(self):
        self.prepare()
        if self.missing:
            self.skipTest(self.missing)

    def test_the_harness_ran_every_case_and_none_failed(self):
        code, error, summary = self.outcome
        self.assertRegex(summary or '', r'^\d+\|\d+$',
                         f'the harness did not finish (exit code {code}):\n{error[-3000:]}')
        total, failed = (int(part) for part in summary.split('|'))
        self.assertEqual(total, len(self.results))                            # every case reported exactly once
        failures = [f'{name}: {reason}' for name, (kind, reason) in self.results.items() if kind == 'fail']
        self.assertEqual(failed, 0, '\n'.join(failures))
        self.assertEqual(code, 0, error[-3000:])
        self.assertRegex(self.host, r'^\d+\.\d+\.\d+\|')                      # the PowerShell version is reported

    def test_no_case_skipped_unless_it_needs_another_machine(self):
        skipped = {name for name, (kind, _) in self.results.items() if kind == 'skip'}
        self.assertEqual(skipped, EXPECTED_SKIPS.get(os.name, set()),
                         '\n'.join(f'{name}: {self.results[name][1]}' for name in sorted(skipped)))


def load_tests(loader, tests, pattern):
    SetupHarnessTests.prepare()
    for name, (status, reason) in SetupHarnessTests.results.items():

        def case(self, name=name, status=status, reason=reason):
            if status == 'skip':
                self.skipTest(reason)
            self.assertEqual(status, 'pass', reason)

        setattr(SetupHarnessTests, 'test_' + name.replace('-', '_'), case)
    return loader.loadTestsFromTestCase(SetupHarnessTests)


if __name__ == '__main__':
    unittest.main()
