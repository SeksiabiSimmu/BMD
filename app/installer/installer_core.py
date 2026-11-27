#!/usr/bin/env python3
"""Brave Music Downloader 2.5.0: installer / uninstaller engine (stdlib only).

The native launcher only unpacks the bootstrap files and starts PowerShell; Setup.ps1 only
bootstraps a verified portable Python and then runs this file. All risky logic (downloads,
verification, staging, swap, rollback, registry, ownership, uninstall) lives here so it can be
tested on any machine.

Install is transactional:
  1. extract + verify everything into  app.staging-<id>   (the live app is untouched)
  2. rename  app -> app.old-<id>,  app.staging-<id> -> app
  3. register the native host (previous registry values are journaled)
  4. commit: install-manifest.json is replaced and the journal marked committed
Any failure before commit restores the previous app and registry values. If the
process dies (power loss), the journal lets the next run finish or roll back.

Uninstall only removes what install-manifest.json says this app owns, and only
inside the app folder, cache folder and the host registry keys that still point
at this app. Music, settings and history are kept unless explicitly requested.

A UI talks to the engine only through files: `--progress-file` receives JSON lines,
`--cancel-file` stops it at the next safe point, `--result-file` always receives the outcome.
"""
import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import shutil
import stat
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'helper'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bmdfs  # noqa: E402
import payload_container  # noqa: E402

VERSION = '2.5.0'
HOST_NAME = 'com.bravemusic.downloader'
EXTENSION_NAME = 'Brave Music Downloader'
# 2.3.0 and earlier shipped the extension as "Brave Music Downloader (WAV)". An upgrade
# over such an install must still be recognized, or setup would refuse to replace it.
LEGACY_EXTENSION_NAMES = {'Brave Music Downloader (WAV)'}
APP_ENTRIES = ('extension', 'helper', 'bridge', 'installer', 'python', 'tools',
               'README.md', 'Uninstall.ps1', 'tools.lock.json')
TOOL_FILES = ('yt-dlp.exe', 'deno.exe', 'ffmpeg.exe', 'ffprobe.exe')
REQUIRED_PAYLOAD = ('extension/manifest.json', 'helper/native_host.py', 'helper/helper.py',
                    'helper/bmdfs.py', 'bridge/NativeLauncher.cs', 'installer/installer_core.py',
                    'installer/payload_container.py', 'README.md', 'Uninstall.ps1', 'tools.lock.json')
BROWSER_KEYS = {
    'Chrome': 'Software\\Google\\Chrome\\NativeMessagingHosts\\' + HOST_NAME,
    'Brave': 'Software\\BraveSoftware\\Brave-Browser\\NativeMessagingHosts\\' + HOST_NAME,
}
# "Programs and Features" entry (per user, like everything else this installer writes).
ARP_KEY = 'Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\BraveMusicDownloader'
START_MENU_FOLDER = 'Brave Music Downloader'
CONFIRM_PHRASE = 'DELETE HISTORY'
MAX_DOWNLOAD = 700 * 1024 * 1024
MAX_UNPACKED = 2 * 1024 ** 3
SHA256 = re.compile(r'^[0-9a-f]{64}$')
EXTENSION_CONTACT_FRESH_SECONDS = 90      # the extension calls the helper at least once a minute
# Setup runs without a console window; every program it starts would otherwise get one of its own.
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
_log_file = None                          # set by main(): a copy of every log line goes here


class InstallError(Exception):
    """Setup could not complete; the previous state has been restored.

    `category` is the machine-readable reason the UI and the exit code are derived from.
    """
    category = 'failed'


class UnsupportedSystem(InstallError):
    category = 'unsupported'


class BraveMissing(InstallError):
    category = 'brave-missing'


class DownloadFailed(InstallError):
    category = 'download'


class SetupCancelled(InstallError):
    category = 'cancelled'


class DowngradeRefused(InstallError):
    category = 'downgrade'


class SetupAlreadyRunning(InstallError):
    category = 'busy'


class InstallerDamaged(InstallError):
    category = 'damaged'


class NotInstalled(InstallError):
    category = 'not-installed'


class SimulatedCrash(BaseException):
    """Test hook: dies like a power cut, bypassing rollback handlers."""


def log(message):
    print(message, flush=True)
    if _log_file is not None:
        try:                              # for display only: a log that cannot be written never stops setup
            with open(_log_file, 'a', encoding='utf-8', errors='replace') as handle:
                handle.write(message + '\n')
        except OSError:
            pass


class Control:
    """The engine's side of the UI channel: progress events out, a cancel request in.

    Both files are ordinary files the parent process chose. A missing or unwritable progress
    file never stops setup (it is only for display); the cancel file is a flag whose mere
    existence means "stop at the next safe point".
    """

    def __init__(self, progress_file=None, cancel_file=None, throttle=0.2):
        self.progress_file = Path(progress_file) if progress_file else None
        self.cancel_file = Path(cancel_file) if cancel_file else None
        self.throttle = throttle
        self.last_emit = 0.0

    def emit(self, event, **fields):
        if not self.progress_file:
            return
        try:
            self.last_emit = time.monotonic()
            line = json.dumps(dict(event=event, time=round(self.last_emit, 3), **fields))
            with open(self.progress_file, 'a', encoding='utf-8') as handle:
                handle.write(line + '\n')
        except OSError:
            pass

    def throttled(self, event, **fields):
        if not self.progress_file or time.monotonic() - self.last_emit >= self.throttle:
            self.emit(event, **fields)

    def cancelled(self):
        return bool(self.cancel_file and self.cancel_file.exists())

    def check_cancel(self):
        if self.cancelled():
            raise SetupCancelled('Setup was cancelled. Nothing was changed; the previous '
                                 'installation was restored.')


QUIET = Control()


# ------------------------------------------------------------------ layout ----
class Layout:
    def __init__(self, root, data_dir=None, output=None):
        self.root = Path(root)
        self.app = self.root / 'app'
        self.cache = self.root / 'cache'
        self.downloads = self.cache / 'downloads'
        self.manifest = self.root / 'install-manifest.json'
        self.pending = self.root / 'install-manifest.json.pending'
        self.journal = self.root / 'install-journal.json'
        self.lockfile = self.root / '.install.lock'
        appdata = os.environ.get('APPDATA') or str(Path.home())
        self.data_dir = Path(data_dir) if data_dir else Path(appdata) / 'BraveMusicDownloader'
        self.output = Path(output) if output else Path.home() / 'Music' / 'Brave Music Downloader'
        self.start_menu = (Path(appdata) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / START_MENU_FOLDER
                           if os.environ.get('APPDATA') else None)

    @property
    def native_manifest(self):
        return self.app / 'bridge' / (HOST_NAME + '.json')

    @property
    def launcher(self):
        return self.app / 'bridge' / 'BraveMusicNativeHost.exe'


# ---------------------------------------------------------------- registry ----
class FakeRegistry:
    def __init__(self):
        self.keys = {}          # key -> default value   (what the browsers read)
        self.values = {}        # key -> {value name: value}   (named values, e.g. the Programs and Features entry)

    def get(self, key):
        return self.keys.get(key)

    def set(self, key, value):
        self.keys[key] = value

    def get_values(self, key):
        return dict(self.values[key]) if key in self.values else None

    def set_values(self, key, values):
        self.values.setdefault(key, {}).update(values)

    def delete(self, key):
        self.keys.pop(key, None)
        self.values.pop(key, None)


class WinRegistry:
    def __init__(self):
        import winreg
        self.w = winreg

    def get(self, key):
        try:
            with self.w.OpenKey(self.w.HKEY_CURRENT_USER, key) as handle:
                return self.w.QueryValue(handle, '')
        except FileNotFoundError:
            return None

    def set(self, key, value):
        with self.w.CreateKey(self.w.HKEY_CURRENT_USER, key) as handle:
            self.w.SetValue(handle, '', self.w.REG_SZ, value)

    def get_values(self, key):
        try:
            with self.w.OpenKey(self.w.HKEY_CURRENT_USER, key) as handle:
                count = self.w.QueryInfoKey(handle)[1]
                return {self.w.EnumValue(handle, i)[0]: self.w.EnumValue(handle, i)[1] for i in range(count)}
        except FileNotFoundError:
            return None

    def set_values(self, key, values):
        with self.w.CreateKey(self.w.HKEY_CURRENT_USER, key) as handle:
            for name, value in values.items():
                if isinstance(value, int):
                    self.w.SetValueEx(handle, name, 0, self.w.REG_DWORD, value)
                else:
                    self.w.SetValueEx(handle, name, 0, self.w.REG_SZ, str(value))

    def delete(self, key):
        try:
            self.w.DeleteKey(self.w.HKEY_CURRENT_USER, key)
        except FileNotFoundError:
            pass
        except OSError:
            # A key with sub-keys cannot go in one call; remove the sub-keys first.
            try:
                with self.w.OpenKey(self.w.HKEY_CURRENT_USER, key, 0, self.w.KEY_ALL_ACCESS) as handle:
                    while True:
                        self.w.DeleteKey(handle, self.w.EnumKey(handle, 0))
            except OSError:
                pass
            try:
                self.w.DeleteKey(self.w.HKEY_CURRENT_USER, key)
            except OSError:
                pass


def same_path(a, b):
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))


# -------------------------------------------------------------------- lock ----
_pid_alive = bmdfs.pid_alive


class InstallLock:
    """Single installer instance; a dead owner's lock is taken over."""

    def __init__(self, layout):
        self.path = layout.lockfile

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    pid = int(self.path.read_text().strip() or 0)
                except (OSError, ValueError):
                    pid = 0
                if pid and _pid_alive(pid):
                    raise SetupAlreadyRunning('Another Brave Music Downloader setup is already running.') from None
                try:
                    self.path.unlink()
                except OSError:
                    pass
        raise InstallError('Could not take the installer lock.')

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- download ----
def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def check_url(url):
    parts = urlsplit(url)
    loopback = parts.hostname in ('127.0.0.1', 'localhost', '::1')
    if parts.scheme != 'https' and not (parts.scheme == 'http' and loopback):
        raise InstallError('Refusing non-HTTPS download URL: ' + url)
    return url


HEX64 = re.compile(r'(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])')


def _names_in(line):
    """File names mentioned on a checksum line ('hash  name', '*name', '(name)', 'Path : D:\\x\\name')."""
    return [re.split(r'[\\/]', token.strip('*()'))[-1].lower() for token in line.split()]


def parse_checksum_text(text, filename=None):
    """Read a publisher checksum file in any common layout.

    Handles sha256sum ('HASH  name' / 'HASH *name'), BSD ('SHA256 (name) = HASH'), a bare hash,
    and PowerShell Get-FileHash output (table or list, any case, CRLF). When the file lists several
    different hashes the line naming `filename` is used; otherwise a single distinct hash is accepted.
    """
    lines = [(line, [h.lower() for h in HEX64.findall(line)]) for line in text.splitlines()]
    if filename:
        wanted = filename.lower()
        for line, hashes in lines:
            if hashes and wanted in _names_in(line):
                return hashes[0]
    distinct = {h for _, hashes in lines for h in hashes}
    if len(distinct) == 1:
        return next(iter(distinct))
    seen = ' '.join(text.split())[:160]
    raise InstallError(f'Checksum file did not contain a usable SHA-256 value for {filename or "the download"} '
                       f'(file started with: {seen!r})')


class _FileResponse:
    """Minimal response object over a file downloaded by Windows itself."""
    status = 200

    def __init__(self, path, folder):
        self._file = open(path, 'rb')
        self._folder = folder
        self.headers = {'Content-Length': str(os.path.getsize(path))}

    def read(self, amount=-1):
        return self._file.read(amount)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self._file.close()
        shutil.rmtree(self._folder, ignore_errors=True)


def on_windows():
    return os.name == 'nt'


def is_tls_trust_error(error):
    reason = getattr(error, 'reason', error)
    return isinstance(reason, ssl.SSLError) or isinstance(error, ssl.SSLError)


def powershell_open(request, timeout=900):
    """Fetch with Windows' own TLS stack (it downloads missing intermediate certificates on demand).

    This is not a downgrade: certificates are still verified, by Windows instead of the bundled
    Python, which cannot fetch an intermediate certificate that a server forgot to send.
    """
    url = check_url(request.full_url)
    folder = tempfile.mkdtemp(prefix='bmd-fetch-')
    target = os.path.join(folder, 'download.bin')
    script = ("$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
              "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; "
              "Invoke-WebRequest -UseBasicParsing -Uri $env:BMD_FETCH_URL -OutFile $env:BMD_FETCH_OUT "
              "-MaximumRedirection 10 -TimeoutSec 900")
    env = dict(os.environ, BMD_FETCH_URL=url, BMD_FETCH_OUT=target)       # never spliced into the command
    try:
        run = subprocess.run(['powershell.exe', '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                              '-Command', script], capture_output=True, text=True, env=env, timeout=timeout * 2,
                             creationflags=NO_WINDOW)
        if run.returncode or not os.path.isfile(target):
            raise urllib.error.URLError('Windows could not fetch it either: ' + (run.stderr or run.stdout)[-300:])
        return _FileResponse(target, folder)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def default_opener(request, timeout=60):
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except (urllib.error.URLError, ssl.SSLError) as exc:
        if on_windows() and is_tls_trust_error(exc):
            log('  The bundled Python could not validate this site\'s certificate chain; '
                'retrying through Windows (still verified): ' + request.full_url)
            return powershell_open(request)
        raise


def fetch_text(url, opener=default_opener, attempts=3, delay=1.0):
    check_url(url)
    request = urllib.request.Request(url, headers={'User-Agent': 'BraveMusicDownloader-Installer/' + VERSION})
    last = None
    for attempt in range(attempts):
        try:
            with opener(request, timeout=60) as response:
                return response.read(1024 * 1024).decode('utf-8', 'replace')
        except urllib.error.HTTPError as exc:
            last = exc
            if 400 <= exc.code < 500:
                break
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            last = exc
        if attempt + 1 < attempts:            # no pause after the last try: nothing follows it
            time.sleep(delay * (attempt + 1))
    raise DownloadFailed(f'Could not download {url} ({last}). Check your internet connection and security '
                         'software, then run setup again.')


def resolve_expected(entry, opener=default_opener, allow_unverified=False):
    """Return (sha256 or None, how it was verified)."""
    if entry.get('sha256'):
        digest = str(entry['sha256']).lower()
        if not SHA256.fullmatch(digest):
            raise InstallError(f"{entry['name']}: pinned hash is not a SHA-256 value")
        return digest, 'pinned'
    source = entry.get('checksum')
    if source:
        text = fetch_text(source['url'], opener)
        return parse_checksum_text(text, source.get('filename')), 'publisher'
    if entry.get('authenticode'):
        return None, 'authenticode'
    if allow_unverified:
        return None, 'unverified'
    raise InstallError(f"{entry['name']}: no pinned hash or publisher checksum; refusing to install. "
                       'Run scripts/refresh_tool_lock.py or pass --allow-unverified.')


def download_verified(url, destination, expected, opener=default_opener, attempts=3, delay=1.0, control=QUIET):
    """Download with resume + hash check; a verified file already on disk is reused.

    `control` (optional) receives byte progress and is asked for cancellation while downloading.
    """
    check_url(url)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        if expected and sha256_file(destination) == expected:
            return sha256_file(destination)
        if not expected:
            return sha256_file(destination)
        destination.unlink()
    partial = destination.with_name(destination.name + '.partial')
    last, completed = None, False
    for attempt in range(1, attempts + 1):
        try:
            offset = partial.stat().st_size if partial.exists() else 0
            request = urllib.request.Request(url, headers={'User-Agent': 'BraveMusicDownloader-Installer/' + VERSION})
            if offset:
                request.add_header('Range', f'bytes={offset}-')
            with opener(request, timeout=60) as response:
                status = getattr(response, 'status', 200)
                mode = 'ab' if offset and status == 206 else 'wb'
                total = offset if mode == 'ab' else 0
                announced = response.headers.get('Content-Length')
                received = 0
                with open(partial, mode) as out:
                    while True:
                        control.check_cancel()
                        chunk = response.read(1024 * 256)
                        if not chunk:
                            break
                        total += len(chunk)
                        received += len(chunk)
                        if total > MAX_DOWNLOAD:
                            raise DownloadFailed('Download is larger than expected; aborting.')
                        out.write(chunk)
                        control.throttled('download', url=url, received=total, resume_from=offset,
                                          announced=announced)
                    out.flush()
                    os.fsync(out.fileno())
                if announced is not None and received < int(announced):
                    # http.client reports a dropped connection as a normal end of data.
                    raise http.client.IncompleteRead(b'', int(announced) - received)
            completed = True
            control.emit('downloaded', url=url, bytes=total)
            break
        except InstallError:
            partial.unlink(missing_ok=True)
            raise
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code == 416:               # the partial file is complete, or unusable
                if expected and partial.exists() and sha256_file(partial) == expected:
                    completed = True
                    break
                partial.unlink(missing_ok=True)
                continue
            if 400 <= exc.code < 500:
                break
            if attempt < attempts:            # no pause after the last try: nothing follows it
                time.sleep(delay * attempt)
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            last = exc
            if attempt < attempts:
                time.sleep(delay * attempt)
    if not completed:
        raise DownloadFailed(f'Download failed ({last}). Check your internet connection and run setup again.')
    actual = sha256_file(partial)
    if expected and actual != expected:
        partial.unlink()
        raise DownloadFailed(f'Checksum mismatch for {url}: expected {expected[:12]}..., got {actual[:12]}.... '
                             'The download was discarded.')
    os.replace(partial, destination)
    return actual


def version_key(text):
    """'2.10.0' -> ((2, 10, 0), ''); '2.5.0-beta.1' -> ((2, 5, 0), 'beta.1'); unparsable -> ((), text)."""
    text = str(text or '').strip()
    match = re.match(r'^v?(\d+(?:\.\d+)*)(?:[-+._\s](.*))?$', text, re.I)
    if not match:
        return (), text.lower()
    numbers = tuple(int(part) for part in match.group(1).split('.'))
    return numbers, (match.group(2) or '').strip().lower()


def compare_versions(a, b):
    """-1, 0 or 1. Numeric part first (2.10 > 2.9); with equal numbers a release sorts after its
    pre-releases (2.5.0 > 2.5.0-beta); an unparsable version never counts as newer."""
    a_numbers, a_suffix = version_key(a)
    b_numbers, b_suffix = version_key(b)
    width = max(len(a_numbers), len(b_numbers))
    a_padded = a_numbers + (0,) * (width - len(a_numbers))
    b_padded = b_numbers + (0,) * (width - len(b_numbers))
    if not a_padded or not b_padded:
        return 0 if str(a).strip() == str(b).strip() else (1 if not b_padded else -1)
    if a_padded != b_padded:
        return 1 if a_padded > b_padded else -1
    if a_suffix == b_suffix:
        return 0
    if not a_suffix or not b_suffix:
        return 1 if not a_suffix else -1
    return 1 if a_suffix > b_suffix else -1


def bundled_copy(bundle, name, archive, expected, control=QUIET):
    """Copy a tool archive out of the installer file (`--bundle`) and verify it.

    Only used when the lock pins an exact SHA-256: the offline installer is built from a pinned
    lock, so the pinned hash, the entry hash and the file content have to agree. A mismatch means
    the installer file itself is damaged, which is reported as such instead of silently falling
    back to a download.
    """
    import payload_container
    archive = Path(archive)
    if archive.is_file() and sha256_file(archive) == expected:
        return expected
    entry = bundle['entries'][name]
    label = 'copying ' + name
    control.emit('component', name=name, status=label)
    try:
        payload_container.copy_entry(bundle['path'], entry, archive, expected,
                                     report=lambda done, total: control.throttled(
                                         'download', url='bundle:' + name, received=done, resume_from=0,
                                         announced=total))
    except payload_container.ContainerError as exc:
        raise InstallerDamaged(str(exc)) from None
    control.emit('downloaded', url='bundle:' + name, bytes=entry['size'])
    return expected


# ---------------------------------------------------------------- unpacking ----
def safe_extract(archive, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    total = 0
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if len(members) > 30000:
            raise InstallError('Archive has too many entries.')
        for member in members:
            name = member.filename.replace('\\', '/')
            if name.startswith('/') or re.match(r'^[A-Za-z]:', name) or '..' in name.split('/'):
                raise InstallError('Unsafe path in archive: ' + member.filename)
            if stat.S_ISLNK(member.external_attr >> 16):
                raise InstallError('Archive contains a link: ' + member.filename)
            target = destination / name
            bmdfs.require_within(target, destination)
            total += member.file_size
            if total > MAX_UNPACKED:
                raise InstallError('Archive expands to an unexpected size.')
        bundle.extractall(destination)


def find_unique(folder, basename):
    matches = [p for p in Path(folder).rglob('*') if p.is_file() and p.name.lower() == basename.lower()]
    if len(matches) != 1:
        raise InstallError(f'Expected exactly one {basename} in the archive, found {len(matches)}.')
    return matches[0]


def patch_pth(python_dir):
    pth = sorted(Path(python_dir).glob('*._pth'))
    if not pth:
        raise InstallError('Embedded Python path configuration is missing.')
    lines = pth[0].read_text(encoding='utf-8').splitlines()
    for extra in ('..\\helper', '..\\installer'):
        if extra not in lines:
            lines.append(extra)
    pth[0].write_text('\n'.join(lines) + '\n', encoding='utf-8')


def extension_id_from_key(key_b64):
    digest = hashlib.sha256(base64.b64decode(key_b64)).digest()[:16]
    return ''.join('abcdefghijklmnop'[b >> 4] + 'abcdefghijklmnop'[b & 15] for b in digest)


# ---------------------------------------------------------------- manifest ----
def load_manifest(layout):
    data = bmdfs.read_json(layout.manifest)
    return data if isinstance(data, dict) and data.get('product') == 'BraveMusicDownloader' else None


def recognized_app(app):
    """True when app/ looks like a folder this product created (any version)."""
    manifest = bmdfs.read_json(Path(app) / 'extension' / 'manifest.json')
    name = manifest.get('name') if isinstance(manifest, dict) else None
    return ((name == EXTENSION_NAME or name in LEGACY_EXTENSION_NAMES)
            and (Path(app) / 'bridge').is_dir())


def synthesize_manifest(layout):
    """Ownership record for a pre-2.3 install that never wrote one."""
    if not recognized_app(layout.app):
        return None
    comps = []
    if (layout.app / 'python').is_dir():
        comps.append({'name': 'python', 'owned': True, 'adopted': True, 'path': 'python'})
    for tool in TOOL_FILES:
        if (layout.app / 'tools' / tool).is_file():
            comps.append({'name': tool[:-4], 'owned': True, 'adopted': True, 'path': 'tools/' + tool})
    return {'product': 'BraveMusicDownloader', 'schema': 1, 'version': 'legacy', 'adopted_legacy': True,
            'components': comps, 'owned_entries': [e for e in APP_ENTRIES if (layout.app / e).exists()],
            'native_host': {'manifest': str(layout.native_manifest),
                            'registry': [{'browser': b, 'key': k} for b, k in BROWSER_KEYS.items()]}}


# ---------------------------------------------------------------- install ----
def _hook(hooks, name):
    if hooks and name in hooks:
        hooks[name]()


def stop_processes_under(folder):
    """Kill processes whose executable lives in `folder` (a running helper, yt-dlp, ffmpeg...).

    The browser restarts its native helper whenever the connection drops, so this is repeated right
    before every rename attempt instead of once at the start.
    """
    if not on_windows():
        return
    script = ("Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and "
              "$_.Path.StartsWith($env:BMD_STOP_UNDER + '\\', [StringComparison]::OrdinalIgnoreCase) } | "
              "ForEach-Object { try { $_.Kill() } catch { } }")
    try:
        subprocess.run(['powershell.exe', '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', script],
                       env=dict(os.environ, BMD_STOP_UNDER=str(folder)), capture_output=True, timeout=60,
                       creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        pass


def locking_processes(folder, limit=600):
    """Best-effort names of programs holding files open under `folder` (Windows Restart Manager)."""
    if not on_windows():
        return []
    try:
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [('lo', wintypes.DWORD), ('hi', wintypes.DWORD)]

        class UNIQUE(ctypes.Structure):
            _fields_ = [('pid', wintypes.DWORD), ('start', FILETIME)]

        class INFO(ctypes.Structure):
            _fields_ = [('process', UNIQUE), ('name', wintypes.WCHAR * 256), ('service', wintypes.WCHAR * 64),
                        ('kind', wintypes.DWORD), ('status', wintypes.ULONG), ('session', wintypes.DWORD),
                        ('restartable', wintypes.BOOL)]

        rm = ctypes.WinDLL('rstrtmgr')
        session, key = wintypes.DWORD(), (wintypes.WCHAR * 33)()
        if rm.RmStartSession(ctypes.byref(session), 0, key):
            return []
        try:
            files = [str(p) for p in Path(folder).rglob('*') if p.is_file()][:limit]
            if not files or rm.RmRegisterResources(session, len(files), (wintypes.LPCWSTR * len(files))(*files),
                                                   0, None, 0, None):
                return []
            needed, have, reasons = wintypes.UINT(0), wintypes.UINT(0), wintypes.DWORD(0)
            code = rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(have), None, ctypes.byref(reasons))
            if code not in (0, 234) or not needed.value:                 # 234 = more data
                return []
            info, have = (INFO * needed.value)(), wintypes.UINT(needed.value)
            if rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(have), info, ctypes.byref(reasons)):
                return []
            return sorted({info[i].name for i in range(have.value)})
        finally:
            rm.RmEndSession(session)
    except Exception:  # diagnostics only; never let this break setup
        return []


def _replace_dir(source, destination, retries, delay=0.5, before_retry=None, describe_lock=None):
    for attempt in range(retries):
        try:
            os.rename(source, destination)
            return
        except PermissionError:
            if attempt == retries - 1:
                who = describe_lock() if describe_lock else []
                raise InstallError('Files are in use' + (f' by: {", ".join(who)}' if who else '') +
                                   '. Close Brave completely (also check the system tray) and any running Brave '
                                   'Music Downloader helper, then run setup again.') from None
            if before_retry:
                before_retry()
            time.sleep(delay)


def _remove_quiet(path, boundary):
    try:
        bmdfs.safe_rmtree(path, boundary)
    except (OSError, bmdfs.BoundaryError) as exc:
        log(f'  (could not remove {path}: {exc})')


def rollback(layout, registry, journal, reason='Rolling back the interrupted installation…'):
    """Undo a half-finished install using only the journal and what is on disk."""
    log(reason)
    for entry in reversed(journal.get('registry', [])):
        if 'values' in entry:                      # a key with named values (Programs and Features entry)
            previous = entry.get('previous_values')
            if previous is None:
                registry.delete(entry['key'])
            else:
                registry.set_values(entry['key'], previous)
            continue
        current = registry.get(entry['key'])
        if entry.get('previous') is None:
            if current is not None and same_path(current, entry.get('new', '')):
                registry.delete(entry['key'])
        else:
            registry.set(entry['key'], entry['previous'])
    old = Path(journal['old']) if journal.get('old') else None
    if journal.get('state') in ('swapping', 'swapped', 'registered'):
        if journal.get('had_previous'):
            if old and old.exists():
                if layout.app.exists():
                    _remove_quiet(layout.app, layout.root)
                os.rename(old, layout.app)
        elif layout.app.exists():
            _remove_quiet(layout.app, layout.root)
    for leftover in (journal.get('staging'),):
        if leftover and Path(leftover).exists():
            _remove_quiet(leftover, layout.root)
    layout.pending.unlink(missing_ok=True)
    layout.journal.unlink(missing_ok=True)


def finish_committed(layout, journal):
    if layout.pending.exists():
        os.replace(layout.pending, layout.manifest)
    for leftover in (journal.get('old'), journal.get('staging')):
        if leftover and Path(leftover).exists():
            _remove_quiet(leftover, layout.root)
    layout.journal.unlink(missing_ok=True)


def recover_interrupted(layout, registry):
    journal = bmdfs.read_json(layout.journal)
    if not isinstance(journal, dict):
        if layout.journal.exists():
            layout.journal.unlink()
        return None
    if journal.get('state') == 'committed':
        finish_committed(layout, journal)
        return 'finished'
    rollback(layout, registry, journal)
    return 'rolled back'


def sweep_stale_dirs(layout):
    for pattern in ('app.staging-*', 'app.old-*', 'app.failed-*'):
        for path in layout.root.glob(pattern):
            _remove_quiet(path, layout.root)


def build_launcher_windows(source, destination):
    """Compile the tiny native launcher with the .NET Framework that ships with Windows."""
    windir = os.environ.get('WINDIR', r'C:\Windows')
    csc = Path(windir) / 'Microsoft.NET' / 'Framework64' / 'v4.0.30319' / 'csc.exe'
    if csc.is_file():
        run = subprocess.run([str(csc), '/nologo', '/target:exe', '/optimize+', f'/out:{destination}', str(source)],
                             capture_output=True, text=True, creationflags=NO_WINDOW)
    else:
        script = ("Add-Type -TypeDefinition (Get-Content -Raw -LiteralPath '%s') "
                  "-OutputAssembly '%s' -OutputType ConsoleApplication" % (source, destination))
        run = subprocess.run(['powershell.exe', '-NoProfile', '-Command', script], capture_output=True, text=True,
                             creationflags=NO_WINDOW)
    if run.returncode or not Path(destination).is_file():
        raise InstallError('Native helper compilation failed: ' + (run.stdout + run.stderr)[-500:])


def native_roundtrip(command, env=None, timeout=20, expect_version=VERSION):
    """Talk to a native-messaging host exactly like the browser does: framed JSON over stdin/stdout.

    Sends one `health` request and waits for the reply. Catches launchers that never flush, hosts that
    crash at start, and hosts that print anything but frames. Returns the reply data.

    The request comes from setup, not from the extension, so the host is told not to record it as
    "the extension is talking to me" (BMD_NO_CONTACT_MARK).
    """
    env = dict(os.environ if env is None else env, BMD_NO_CONTACT_MARK='1')
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, creationflags=NO_WINDOW)
    result = {}
    timed_out = False

    def read_reply():
        try:
            header = _read_exact(proc.stdout, 4)
            size = struct.unpack('<I', header)[0]
            if not 0 < size <= 1024 * 1024:
                raise ValueError(f'bad frame size {size}')
            result['reply'] = json.loads(_read_exact(proc.stdout, size).decode('utf-8'))
        except Exception as exc:  # reported below with the host's own stderr
            result['error'] = f'{type(exc).__name__}: {exc}'

    reader = threading.Thread(target=read_reply, daemon=True)
    reader.start()
    try:
        raw = json.dumps({'id': 1, 'method': 'health'}).encode()
        proc.stdin.write(struct.pack('<I', len(raw)) + raw)
        proc.stdin.flush()                                   # the browser keeps stdin open after a request
        reader.join(timeout)
        if reader.is_alive():
            # A reply that only appears after stdin is closed means the launcher is not forwarding
            # requests; the browser never closes stdin, so that counts as no answer.
            result.pop('reply', None)
            timed_out = True
    except OSError as exc:
        result.setdefault('error', f'could not write the request: {exc}')
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)
    stderr = proc.stderr.read().decode('utf-8', 'replace').strip()[-600:]
    for stream in (proc.stdout, proc.stderr):
        stream.close()
    if timed_out or 'reply' not in result:
        why = (f'no reply within {timeout} s while the connection stayed open' if timed_out
               else result.get('error') or f'no reply within {timeout} s')
        raise InstallError(f'The helper did not answer ({why}).' + (f' Its error output: {stderr}' if stderr else ''))
    reply = result['reply']
    data = reply.get('data') if isinstance(reply, dict) else None
    if not (isinstance(reply, dict) and reply.get('ok') and isinstance(data, dict)):
        raise InstallError(f'The helper replied with an error: {str(reply)[:300]}')
    if expect_version and data.get('version') != expect_version:
        raise InstallError(f"The helper reports version {data.get('version')}, expected {expect_version}.")
    return data


def _read_exact(stream, count):
    chunks = []
    while count:
        chunk = stream.read(count)
        if not chunk:
            raise EOFError('helper closed its output before replying')
        chunks.append(chunk)
        count -= len(chunk)
    return b''.join(chunks)


def default_host_check(layout):
    log('Checking that the helper answers like it will for the browser…')
    # The helper refuses to start while setup holds its lock; this check is setup's own, so allow it.
    data = native_roundtrip([str(layout.launcher)], env=dict(os.environ, BMD_ALLOW_DURING_INSTALL='1'))
    log(f"  ok  helper answered: version {data.get('version')}, saving to {data.get('output')}")


def hostcheck(layout, registry):
    """Diagnose 'Disconnected': registration, manifest, launcher and a live round trip."""
    manifest = load_manifest(layout) or {}
    expected_id = manifest.get('extension_id') or 'lliokngbogdlnnjffdfgdjabpmjpobab'
    lines, ok = [], True

    def check(label, passed, detail=''):
        nonlocal ok
        ok = ok and passed
        lines.append(f"{'ok  ' if passed else 'FAIL'} {label}" + (f' - {detail}' if detail else ''))

    for browser, key in BROWSER_KEYS.items():
        value = registry.get(key)
        check(f'{browser} registration', value is not None and same_path(value, layout.native_manifest),
              f'{key} -> {value}' if value else f'missing: {key}')
    host = bmdfs.read_json(layout.native_manifest)
    check('native host manifest is readable', isinstance(host, dict), str(layout.native_manifest))
    if isinstance(host, dict):
        check('manifest allows this extension', f'chrome-extension://{expected_id}/' in host.get('allowed_origins', []),
              f"allowed: {host.get('allowed_origins')}  (extension ID must be {expected_id})")
        check('manifest points at the launcher', same_path(host.get('path', ''), layout.launcher), str(host.get('path')))
    check('launcher exists', layout.launcher.is_file(), str(layout.launcher))
    paths_file = layout.app / 'bridge' / 'host-paths.txt'
    pair = paths_file.read_text(encoding='utf-8').split('\n')[:2] if paths_file.is_file() else []
    check('host-paths.txt lists python and the host script', len(pair) == 2 and all(Path(p.strip()).is_file() for p in pair),
          ' | '.join(pair) if pair else 'missing')
    try:
        data = native_roundtrip([str(layout.launcher)])
        check('helper answers over native messaging', True, f"version {data.get('version')}")
    except (InstallError, OSError) as exc:
        check('helper answers over native messaging', False, str(exc))
    return {'ok': ok, 'lines': lines}


def smoke_test(staging):
    python = staging / 'python' / 'python.exe'
    checks = [[str(python), '-c', 'import sqlite3, ssl, json, hashlib; print(sqlite3.sqlite_version)'],
              [str(python), '-c', 'import native_host, helper; print(helper.VERSION)'],
              [str(staging / 'tools' / 'yt-dlp.exe'), '--version'],
              [str(staging / 'tools' / 'deno.exe'), '--version'],
              [str(staging / 'tools' / 'ffmpeg.exe'), '-version'],
              [str(staging / 'tools' / 'ffprobe.exe'), '-version']]
    for command in checks:
        run = subprocess.run(command, capture_output=True, text=True, timeout=120, cwd=str(staging / 'helper'),
                             creationflags=NO_WINDOW)
        if run.returncode:
            raise InstallError(f'Startup check failed for {Path(command[0]).name}: {(run.stderr or run.stdout)[-300:]}')
        log('  ok  ' + Path(command[0]).name + '  ' + ((run.stdout or run.stderr).splitlines() or [''])[0][:80])


def extension_contact(data_dir, now=None):
    """What the helper recorded about its last contact with the extension.

    The helper writes a small file whenever the *browser* sends it a request (the extension asks for
    work at least once a minute while Brave runs). Setup's own health checks do not count, so a fresh
    entry means the extension really is loaded and talking. The file is informational: anything
    running as the user could write it, so it is a convenience indicator, not proof of identity.
    """
    record = bmdfs.read_json(Path(data_dir) / 'extension-contact.json')
    now = time.time() if now is None else now
    if not isinstance(record, dict) or not isinstance(record.get('time'), (int, float)):
        return {'seen': False, 'fresh': False, 'age_seconds': None, 'version': None, 'method': None}
    age = max(0.0, now - record['time'])
    return {'seen': True, 'fresh': age <= EXTENSION_CONTACT_FRESH_SECONDS, 'age_seconds': round(age, 1),
            'version': record.get('version'), 'method': record.get('method')}


def powershell_path():
    """The Windows PowerShell every supported Windows has (absolute, so PATH cannot redirect it)."""
    return Path(os.environ.get('SystemRoot') or os.environ.get('WINDIR') or r'C:\Windows') / \
        'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'


def uninstall_command(layout, silent=False):
    command = f'"{powershell_path()}" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "{layout.app / "Uninstall.ps1"}"'
    return command + (' -Silent' if silent else '')


def arp_values(layout, version):
    """The values of the per-user "Programs and Features" entry."""
    size_kb = min(2 ** 32 - 1, bmdfs.dir_size(layout.app) // 1024) if layout.app.is_dir() else 0
    return {'DisplayName': EXTENSION_NAME, 'DisplayVersion': version, 'Publisher': EXTENSION_NAME,
            'DisplayIcon': str(layout.app / 'bridge' / 'setup.ico'),
            'InstallLocation': str(layout.root), 'InstallDate': time.strftime('%Y%m%d'),
            'UninstallString': uninstall_command(layout), 'QuietUninstallString': uninstall_command(layout, True),
            'Comments': 'Local audio processing helper for the Brave Music Downloader browser extension.',
            'NoModify': 1, 'NoRepair': 1, 'EstimatedSize': size_kb}


def register_arp(layout, registry, journal, version):
    previous = registry.get_values(ARP_KEY)
    journal['registry'].append({'key': ARP_KEY, 'values': True, 'previous_values': previous})
    bmdfs.write_json_atomic(layout.journal, journal)
    registry.set_values(ARP_KEY, arp_values(layout, version))


def install(layout, payload_zip, lock, registry, *, extension_id, python_zip=None, python_verified='authenticode',
            opener=default_opener, allow_unverified=False, builder=None, smoke=False, reuse_system=False,
            rename_retries=40, hooks=None, keep_downloads=False, host_check=None, stopper=None,
            allow_downgrade=False, bundle=None, control=QUIET):
    layout.root.mkdir(parents=True, exist_ok=True)
    control.emit('phase', phase='preparing', label='Preparing setup')
    with InstallLock(layout):
        state = recover_interrupted(layout, registry)
        if state:
            log(f'Previous setup was interrupted; {state}.')
        sweep_stale_dirs(layout)
        run_id = uuid.uuid4().hex[:12]
        staging, old = layout.root / f'app.staging-{run_id}', layout.root / f'app.old-{run_id}'
        had_previous = layout.app.exists()
        if had_previous and not recognized_app(layout.app):
            raise InstallError(f'{layout.app} exists but is not a Brave Music Downloader folder; '
                               'refusing to replace it.')
        journal = {'id': run_id, 'state': 'staging', 'staging': str(staging), 'old': str(old),
                   'app': str(layout.app), 'had_previous': had_previous, 'registry': [], 'version': VERSION}
        bmdfs.write_json_atomic(layout.journal, journal)
        try:
            control.emit('phase', phase='installing', label='Installing')
            log('Unpacking application files…')
            safe_extract(payload_zip, staging)
            for required in REQUIRED_PAYLOAD:
                if not (staging / required).is_file():
                    raise InstallError('Invalid application archive: missing ' + required)
            ext_manifest = bmdfs.read_json(staging / 'extension' / 'manifest.json') or {}
            if not ext_manifest.get('key') or extension_id_from_key(ext_manifest['key']) != extension_id:
                raise InstallError('Extension identity check failed; refusing to register the native host.')
            # Smart update: if a previous install-manifest.json exists, hand its recorded
            # component hashes to install_components so an unchanged tool (same pinned hash,
            # files still present) is copied from the old app instead of re-downloaded.
            previous_manifest = load_manifest(layout) if had_previous else None
            previous_components = {}
            previous_version = None
            if previous_manifest:
                previous_version = previous_manifest.get('version')
                for comp in previous_manifest.get('components', []) or []:
                    if isinstance(comp, dict) and comp.get('name') and comp.get('sha256'):
                        previous_components[comp['name']] = comp
            # Downgrade guard: nothing has been downloaded or changed at this point.
            if (previous_version and previous_version != 'legacy' and not allow_downgrade
                    and compare_versions(previous_version, VERSION) > 0):
                raise DowngradeRefused(
                    f'Version {previous_version} is already installed and this setup contains {VERSION}. '
                    f'Nothing was changed. To install an older version on purpose, run setup with '
                    f'-AllowDowngrade (or /ALLOWDOWNGRADE).')
            control.check_cancel()
            components, update_summary = install_components(layout, staging, lock, opener, allow_unverified,
                                                            python_zip, python_verified, reuse_system,
                                                            previous_components=previous_components,
                                                            previous_app=layout.app if had_previous else None,
                                                            bundle=bundle, control=control)
            patch_pth(staging / 'python')
            _hook(hooks, 'staged')
            if previous_version and previous_version != VERSION:
                log(f'Updated {previous_version} -> {VERSION}.')
            if update_summary['reused']:
                log(f'Reused unchanged components: {", ".join(update_summary["reused"])}.')
            if update_summary['repaired']:
                log(f'Repaired missing/changed components: {", ".join(update_summary["repaired"])}.')
            if smoke:
                log('Running startup checks…')
                smoke_test(staging)
            bridge = staging / 'bridge'
            if builder:
                log('Building the native launcher…')
                builder(bridge / 'NativeLauncher.cs', bridge / 'BraveMusicNativeHost.exe')
            final_python, final_host = layout.app / 'python' / 'python.exe', layout.app / 'helper' / 'native_host.py'
            (bridge / 'host-paths.txt').write_text(f'{final_python}\n{final_host}\n', encoding='utf-8')
            host_info = {'name': HOST_NAME, 'description': 'Brave Music Downloader local audio processing helper',
                         'path': str(layout.launcher), 'type': 'stdio',
                         'allowed_origins': [f'chrome-extension://{extension_id}/']}
            (bridge / (HOST_NAME + '.json')).write_text(json.dumps(host_info, indent=2), encoding='utf-8')
            # From here on the live app folder is being replaced; a cancel request is not honoured
            # any more (the transaction finishes or rolls back on its own).
            control.check_cancel()
            control.emit('phase', phase='swapping', label='Activating', critical=True)
            _hook(hooks, 'before_swap')

            journal['state'] = 'swapping'
            bmdfs.write_json_atomic(layout.journal, journal)
            stop = (lambda: stopper(layout.app)) if stopper else None
            lock_info = (lambda: locking_processes(layout.app)) if stopper else None
            if had_previous:
                if stop:
                    stop()
                _replace_dir(layout.app, old, rename_retries, before_retry=stop, describe_lock=lock_info)
            _hook(hooks, 'between_renames')
            _replace_dir(staging, layout.app, rename_retries)
            journal['state'] = 'swapped'
            bmdfs.write_json_atomic(layout.journal, journal)
            _hook(hooks, 'after_swap')
            if host_check:
                host_check(layout)           # a helper that cannot answer rolls the install back

            log('Registering the native messaging helper…')
            registrations = []
            for browser, key in BROWSER_KEYS.items():
                previous = registry.get(key)
                entry = {'browser': browser, 'key': key, 'previous': previous, 'new': str(layout.native_manifest)}
                journal['registry'].append(entry)
                bmdfs.write_json_atomic(layout.journal, journal)
                registry.set(key, str(layout.native_manifest))
                registrations.append({'browser': browser, 'key': key})
            journal['state'] = 'registered'
            bmdfs.write_json_atomic(layout.journal, journal)
            _hook(hooks, 'after_registry')

            register_arp(layout, registry, journal, VERSION)

            manifest = {'product': 'BraveMusicDownloader', 'schema': 1, 'version': VERSION,
                        'installed_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                        'paths': {'root': str(layout.root), 'app': str(layout.app), 'cache': str(layout.cache),
                                  'data': str(layout.data_dir), 'music': str(layout.output)},
                        'extension_id': extension_id, 'components': components,
                        'owned_entries': [e for e in APP_ENTRIES if (layout.app / e).exists()],
                        'native_host': {'manifest': str(layout.native_manifest), 'launcher': str(layout.launcher),
                                        'registry': registrations},
                        'update': {'previous_version': previous_version,
                                   'reused': update_summary['reused'],
                                   'repaired': update_summary['repaired'],
                                   'fresh_install': not had_previous}}
            bmdfs.write_json_atomic(layout.pending, manifest)
            _hook(hooks, 'before_commit')
            journal['state'] = 'committed'                 # the commit point
            bmdfs.write_json_atomic(layout.journal, journal)
            finish_committed(layout, journal)
            if not keep_downloads:
                _remove_quiet(layout.downloads, layout.root)
        except Exception:
            rollback(layout, registry, bmdfs.read_json(layout.journal) or journal,
                     'Setup did not finish; restoring the previous state…')
            raise
        log('Installation complete.')
        control.emit('phase', phase='installed', label='Installed')
        return manifest


def install_components(layout, staging, lock, opener, allow_unverified, python_zip, python_verified, reuse_system,
                        previous_components=None, previous_app=None, bundle=None, control=QUIET):
    """Install/verify each tool component. Returns (records, update_summary).

    Smart update: when `previous_components` (from the prior install-manifest.json) records a
    component's sha256 that matches the lock's expected hash AND the files still exist in
    `previous_app`, the component is copied from the old app into staging instead of being
    re-downloaded and re-extracted. A missing or hash-mismatched component is reinstalled
    (repaired). The summary lists which components were reused vs repaired.

    `bundle` (an offline installer: {'path': exe, 'entries': {name: entry}}) supplies a component
    from the installer file itself instead of the network; it still has to match the pinned hash.
    """
    previous_components = previous_components or {}
    records = []
    reused, repaired = [], []

    def _reuse_from_previous(entry, expected_sha, dest_rels):
        """Copy a component's files from the previous app when the recorded hash matches.

        `extract_to` components (e.g. python) produce a directory; `exe`/`pick` components
        produce individual files. Both are copied verbatim so the reused tool is byte-identical
        to the one the previous install verified."""
        prev = previous_components.get(entry['name'])
        if not prev or not previous_app:
            return False
        if prev.get('sha256', '').lower() != (expected_sha or '').lower():
            return False  # the pinned version changed → reinstall
        for rel in dest_rels:
            src = previous_app / rel
            dst = staging / rel
            if not src.exists():
                return False  # file/dir went missing → repair
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst, dirs_exist_ok=False)
            else:
                shutil.copyfile(src, dst)
        return True

    for entry in lock.get('components', []):
        name = entry['name']
        if reuse_system and name != 'python':
            found = shutil.which(name)
            if found:
                log(f'Using existing {name}: {found} (not owned by this app)')
                records.append({'name': name, 'owned': False, 'source': 'system', 'path': found})
                continue
        # Resolve the expected hash first; reuse needs it even before downloading.
        if name == 'python' and python_zip:
            archive, how = Path(python_zip), python_verified
            expected = sha256_file(archive)
        else:
            expected, how = resolve_expected(entry, opener, allow_unverified)
            if how == 'authenticode' and not expected:
                raise InstallError(f'{name} can only be verified by Setup.ps1 (Windows code-signing check); '
                                   'pin its sha256 in tools.lock.json to install it directly.')
            archive = layout.downloads / (name + '-' + (expected or 'unverified')[:16] + '.bin')
        # Determine the destination relative paths this component produces, so reuse can copy them.
        if entry.get('kind') == 'exe':
            dest_rels = [entry['provides'][0]]
        elif entry.get('extract_to'):
            dest_rels = [entry['extract_to']]
        else:
            dest_rels = list(entry.get('pick', {}).values())
        # Try to reuse the unchanged component from the previous install.
        if _reuse_from_previous(entry, expected, dest_rels):
            log(f'Reusing {name} {entry.get("version", "")} from the previous install (unchanged).')
            reused.append(name)
            files = dest_rels
            digest = expected
            source = 'previous'
        else:
            control.check_cancel()
            kind = 'repaired' if name in previous_components else 'installed'
            status = f'{"Repairing" if kind == "repaired" else "Installing"} {name} {entry.get("version", "")}…'
            control.emit('component', name=name, status=status)
            log(status)
            files = []
            if name == 'python' and python_zip:
                digest = expected  # the local python zip's hash, computed above
                source = 'setup'
            elif bundle and expected and name in bundle.get('entries', {}):
                digest = bundled_copy(bundle, name, archive, expected, control)
                source = 'bundle'
            else:
                digest = download_verified(entry['url'], archive, expected, opener, control=control)
                source = 'download'
            if entry.get('kind') == 'exe':
                target = staging / entry['provides'][0]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(archive, target)
                files.append(entry['provides'][0])
            elif entry.get('extract_to'):
                safe_extract(archive, staging / entry['extract_to'])
                files.append(entry['extract_to'])
            else:
                scratch = layout.cache / 'unpack' / (name + '-' + uuid.uuid4().hex[:8])
                safe_extract(archive, scratch)
                for source_name, destination in entry['pick'].items():
                    target = staging / destination
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(find_unique(scratch, source_name), target)
                    files.append(destination)
                _remove_quiet(scratch, layout.root)
            if kind == 'repaired':
                repaired.append(name)
        for rel in entry.get('provides', []):
            if not (staging / rel).exists():
                raise InstallError(f'{name}: expected file {rel} was not produced.')
        records.append({'name': name, 'version': entry.get('version', ''), 'kind': entry.get('kind'),
                        'url': entry['url'], 'sha256': digest, 'verified_by': how, 'owned': True,
                        'source': source,
                        'path': files[0] if len(files) == 1 else entry.get('extract_to') or files[0],
                        'files': files})
    return records, {'reused': reused, 'repaired': repaired}


# --------------------------------------------------------------- uninstall ----
class Actions:
    def __init__(self, dry_run):
        self.dry_run = dry_run
        self.removed, self.kept, self.skipped, self.failed = [], [], [], []

    def do(self, label, function, soft_note=None):
        if self.dry_run:
            self.removed.append('[dry run] ' + label)
            return
        try:
            function()
            self.removed.append(label)
        except Exception as exc:  # keep going; report every failure at the end
            if soft_note:
                self.kept.append(f'{label}: {soft_note}')
            else:
                self.failed.append(f'{label}: {exc}')


def _owned_rel(rel):
    rel = str(rel).replace('\\', '/')
    parts = [p for p in rel.split('/') if p]
    if not parts or '..' in parts or rel.startswith('/') or re.match(r'^[A-Za-z]:', rel):
        return None
    return parts


def uninstall(layout, registry, *, remove_history=False, confirm=None, dry_run=False):
    if remove_history and confirm != CONFIRM_PHRASE:
        raise InstallError(f'Removing history requires --confirm "{CONFIRM_PHRASE}".')
    if dry_run or not layout.root.exists():
        return _uninstall(layout, registry, remove_history, dry_run)
    with InstallLock(layout):
        if recover_interrupted(layout, registry):
            log('Finished or rolled back an interrupted setup first.')
        return _uninstall(layout, registry, remove_history, dry_run)


def _uninstall(layout, registry, remove_history, dry_run):
    manifest = load_manifest(layout)
    legacy = False
    if manifest is None:
        manifest = synthesize_manifest(layout)
        legacy = manifest is not None
        if legacy:
            log('No ownership record found; treating this as a pre-2.3 installation.')
    acts = Actions(dry_run)
    app = layout.app
    expected_manifest = (manifest or {}).get('native_host', {}).get('manifest') or str(layout.native_manifest)

    # Programs and Features entry, only when it is ours (it names this installation's folder).
    entry = registry.get_values(ARP_KEY)
    if entry is not None:
        ours = (same_path(entry.get('InstallLocation', ''), layout.root)
                or str(app / 'Uninstall.ps1').lower() in str(entry.get('UninstallString', '')).lower())
        if ours:
            acts.do('Programs and Features entry', lambda: registry.delete(ARP_KEY))
        else:
            acts.skipped.append(f'Programs and Features entry: belongs to another installation '
                                f'({entry.get("InstallLocation")}); left alone')
    if layout.start_menu is not None:
        folder = layout.start_menu
        if os.path.lexists(folder):
            acts.do('Start Menu shortcuts', lambda: bmdfs.safe_rmtree(folder, folder.parent))

    for browser, key in BROWSER_KEYS.items():
        current = registry.get(key)
        if current is None:
            continue
        if same_path(current, expected_manifest):
            acts.do(f'registry key {browser}', lambda k=key: registry.delete(k))
        else:
            acts.skipped.append(f'registry key {browser}: points at another host ({current}); left alone')

    if manifest is None:
        acts.skipped.append('app folder: no ownership record and it is not a recognizable install; left alone')
    else:
        entries = [e for e in manifest.get('owned_entries', []) if e in APP_ENTRIES]
        tool_files = []
        for comp in manifest.get('components', []):
            if not comp.get('owned'):
                acts.kept.append(f"{comp.get('name')}: {comp.get('path', 'system')} (not installed by this app)")
                continue
            for rel in comp.get('files') or [comp.get('path')]:
                parts = _owned_rel(rel or '')
                if parts and parts[0] == 'tools' and len(parts) == 2 and parts[1].lower() in TOOL_FILES:
                    tool_files.append(app.joinpath(*parts))
        for path in tool_files:
            if os.path.lexists(path):
                acts.do(f'tool {path.name}', lambda p=path: bmdfs.safe_unlink(p, app))
        if (app / 'tools').is_dir():
            if not dry_run:
                try:
                    os.rmdir(app / 'tools')
                except OSError:
                    acts.kept.append('tools folder (contains files this app did not install)')
        for entry in entries:
            if entry == 'tools':
                continue
            path = app / entry
            if not os.path.lexists(path):
                continue
            if entry == 'python' and not any(c.get('name') == 'python' and c.get('owned') for c in manifest.get('components', [])):
                acts.kept.append('python (not recorded as installed by this app)')
                continue
            if path.is_dir() and not bmdfs.is_link(path):
                acts.do(f'folder {entry}', lambda p=path: bmdfs.safe_rmtree(p, app))
            else:
                note = 'still running; Uninstall.ps1 removes it as it exits' if entry == 'Uninstall.ps1' else None
                acts.do(f'file {entry}', lambda p=path: bmdfs.safe_unlink(p, app), soft_note=note)
        if app.is_dir() and not dry_run:
            try:
                os.rmdir(app)
            except OSError:
                leftovers = sorted(p.name for p in app.iterdir())
                acts.kept.append('app folder still holds files this app did not create: ' + ', '.join(leftovers[:8]))

    if layout.cache.exists():
        acts.do('temporary cache', lambda: bmdfs.safe_rmtree(layout.cache, layout.root))
    for name in ('musicbrainz-cache', 'native-host.lock', 'extension-contact.json',   # helper's own runtime files
                 'access_token.txt'):                                                 # legacy 2.0.x cache/secret
        path = layout.data_dir / name
        if os.path.lexists(path):
            acts.do(f'legacy {name}', lambda p=path: (bmdfs.safe_rmtree(p, layout.data_dir) if p.is_dir()
                                                      else bmdfs.safe_unlink(p, layout.data_dir)))
    if remove_history:
        for name in ('queue.sqlite3', 'queue.sqlite3-wal', 'queue.sqlite3-shm', 'queue.sqlite3.pre-2.3.bak'):
            path = layout.data_dir / name
            if os.path.lexists(path):
                acts.do(f'history {name}', lambda p=path: bmdfs.safe_unlink(p, layout.data_dir))
        if layout.data_dir.is_dir() and not dry_run:
            try:
                os.rmdir(layout.data_dir)
            except OSError:
                pass
    else:
        acts.kept.append(f'download history and settings: {layout.data_dir}')
    acts.kept.append(f'your music (never touched): {layout.output}')

    if not acts.failed and not dry_run:
        for path in (layout.manifest, layout.pending, layout.journal, layout.lockfile):
            path.unlink(missing_ok=True)
        try:
            os.rmdir(layout.root)
        except OSError:
            pass          # logs remain here
    return {'removed': acts.removed, 'kept': acts.kept, 'skipped': acts.skipped, 'failed': acts.failed,
            'legacy_install': legacy, 'dry_run': dry_run}


def verify(layout, registry=None):
    """Check an installation: record, component files, and (when a registry is given) registration."""
    manifest = load_manifest(layout)
    if not manifest:
        return {'ok': False, 'problems': ['No installation record found.'], 'version': None}
    problems = []
    for comp in manifest.get('components', []):
        if comp.get('owned'):
            for rel in comp.get('files') or []:
                if not (layout.app / rel).exists():
                    problems.append('missing: ' + rel)
    if registry is not None:
        for browser, key in BROWSER_KEYS.items():
            value = registry.get(key)
            if value is None or not same_path(value, layout.native_manifest):
                problems.append(f'{browser} does not point at this installation ({key} -> {value})')
        if not layout.launcher.is_file():
            problems.append('missing: native messaging launcher')
    return {'ok': not problems, 'problems': problems, 'version': manifest.get('version')}


def diagnose(layout, registry, extra=None):
    """A plain report of what works and what does not, for the user, a support request or setup itself.

    Every check runs and is recorded; a failing check never stops the next one. `extra` carries
    facts setup collected itself (the detected browser) so the report has everything in one place.
    """
    import platform
    checks = []

    def check(name, ok, detail=''):
        checks.append({'check': name, 'ok': bool(ok), 'detail': str(detail)})
        log(f"{'ok  ' if ok else 'FAIL'} {name}" + (f' - {detail}' if detail else ''))

    log(f'Diagnosing Brave Music Downloader {VERSION} ({layout.root})')
    manifest = load_manifest(layout)
    check('installation record', manifest is not None,
          f'version {manifest.get("version")}' if manifest else f'nothing is installed under {layout.root}')
    for name, args in (('yt-dlp', ['--version']), ('deno', ['--version']),
                       ('ffmpeg', ['-version']), ('ffprobe', ['-version'])):
        exe = layout.app / 'tools' / (name + '.exe')
        if not exe.is_file():
            check(f'{name} is installed', False, f'missing: {exe}')
            continue
        try:
            run = subprocess.run([str(exe), *args], capture_output=True, text=True, timeout=120,
                                 creationflags=NO_WINDOW)
            first = ((run.stdout or run.stderr).splitlines() or [''])[0][:80]
            check(f'{name} runs', run.returncode == 0, first or f'exit code {run.returncode}')
        except (OSError, subprocess.TimeoutExpired) as exc:
            check(f'{name} runs', False, f'{type(exc).__name__}: {exc}')
    for browser, key in BROWSER_KEYS.items():
        value = registry.get(key)
        check(f'{browser} native messaging registration', value is not None and same_path(value, layout.native_manifest),
              f'-> {value}' if value else f'missing: {key}')
    host = bmdfs.read_json(layout.native_manifest)
    expected_id = (manifest or {}).get('extension_id') or 'lliokngbogdlnnjffdfgdjabpmjpobab'
    check('native host manifest allows the extension', isinstance(host, dict)
          and f'chrome-extension://{expected_id}/' in host.get('allowed_origins', []), str(layout.native_manifest))
    check('native messaging launcher exists', layout.launcher.is_file(), str(layout.launcher))
    try:
        data = native_roundtrip([str(layout.launcher)])
        check('helper answers over native messaging', True,
              f"version {data.get('version')}, saving to {data.get('output')}")
    except (InstallError, OSError) as exc:
        check('helper answers over native messaging', False, str(exc))
    contact = extension_contact(layout.data_dir)
    check('extension has contacted the helper', contact['seen'],
          f"last contact {contact['age_seconds']} s ago (version {contact['version']})"
          if contact['seen'] else
          f'never; the extension is not loaded in a running browser, or it was loaded less than a minute ago')
    try:
        usage = shutil.disk_usage(layout.root if layout.root.exists() else layout.root.parent)
        check('free disk space', usage.free > 200 * 1024 * 1024, f'{usage.free // (1024 * 1024)} MB free')
    except OSError as exc:
        check('free disk space', False, str(exc))
    try:
        layout.data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=layout.data_dir):
            pass
        check('settings and history folder is writable', True, str(layout.data_dir))
    except OSError as exc:
        check('settings and history folder is writable', False, f'{layout.data_dir}: {exc}')
    check('music folder', layout.output.is_dir(),
          str(layout.output) + ('' if layout.output.is_dir() else ' (created on first download)'))
    return {'ok': all(c['ok'] for c in checks), 'checks': checks, 'version': VERSION,
            'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'environment': {'system': platform.platform(), 'machine': platform.machine(),
                            'python': platform.python_version(), 'root': str(layout.root),
                            'data': str(layout.data_dir), 'music': str(layout.output)},
            'browser': extra or None}


# --------------------------------------------------------------------- CLI ----
def main(argv=None):
    parser = argparse.ArgumentParser(description='Brave Music Downloader installer engine')
    sub = parser.add_subparsers(dest='cmd', required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--root', required=True)
    common.add_argument('--data-dir')
    common.add_argument('--output')
    common.add_argument('--registry', choices=('winreg', 'fake'), default='winreg')
    common.add_argument('--json-report')
    common.add_argument('--result-file', help='always written: ok, exit code, category, message, result')
    common.add_argument('--log-file', help='a copy of every log line goes to this file as well')
    # accepted by every command so a caller passes the same set to each; only install reports progress
    common.add_argument('--progress-file')
    common.add_argument('--cancel-file')
    i = sub.add_parser('install', parents=[common])
    i.add_argument('--payload', required=True)
    i.add_argument('--lock')
    i.add_argument('--extension-id', required=True)
    i.add_argument('--python-zip')
    i.add_argument('--python-verified', default='authenticode')
    i.add_argument('--allow-unverified', action='store_true')
    i.add_argument('--allow-downgrade', action='store_true')
    i.add_argument('--smoke', action='store_true')
    i.add_argument('--build-launcher', action='store_true')
    i.add_argument('--check-host', action='store_true')
    i.add_argument('--reuse-system', action='store_true')
    i.add_argument('--bundle', help='offline installer: take pinned tool archives from this file')
    u = sub.add_parser('uninstall', parents=[common])
    u.add_argument('--remove-history', action='store_true')
    u.add_argument('--confirm')
    u.add_argument('--dry-run', action='store_true')
    sub.add_parser('verify', parents=[common])
    sub.add_parser('hostcheck', parents=[common])
    d = sub.add_parser('diagnose', parents=[common])
    d.add_argument('--extra-json', help='facts collected by setup (e.g. the detected browser) to include')
    args = parser.parse_args(argv)
    global _log_file
    _log_file = args.log_file
    layout = Layout(args.root, args.data_dir, args.output)
    registry = FakeRegistry() if args.registry == 'fake' else WinRegistry()
    outcome = {'ok': False, 'code': 1, 'category': 'failed', 'message': None, 'result': None}
    try:
        if args.cmd == 'install':
            lock_path = Path(args.lock) if args.lock else None
            if lock_path is None:
                with zipfile.ZipFile(args.payload) as bundle:
                    lock = json.loads(bundle.read('tools.lock.json'))
            else:
                lock = json.loads(lock_path.read_text(encoding='utf-8'))
            bundle = None
            if args.bundle:
                try:
                    entries = payload_container.by_name(payload_container.read(args.bundle))
                except payload_container.ContainerError as exc:
                    raise InstallerDamaged(str(exc)) from None
                bundle = {'path': args.bundle, 'entries': entries}
            manifest = install(layout, args.payload, lock, registry, extension_id=args.extension_id,
                               python_zip=args.python_zip, python_verified=args.python_verified,
                               allow_unverified=args.allow_unverified, allow_downgrade=args.allow_downgrade,
                               builder=build_launcher_windows if args.build_launcher else None,
                               smoke=args.smoke, reuse_system=args.reuse_system,
                               host_check=default_host_check if args.check_host else None,
                               stopper=stop_processes_under if on_windows() else None,
                               bundle=bundle,
                               control=Control(args.progress_file, args.cancel_file))
            update = manifest.get('update', {})
            result = {'ok': True, 'version': VERSION, 'root': str(layout.root),
                      'previous_version': update.get('previous_version'),
                      'fresh_install': update.get('fresh_install'),
                      'reused': update.get('reused'), 'repaired': update.get('repaired'),
                      'extension_id': manifest.get('extension_id'),
                      'components': [{'name': c['name'], 'version': c.get('version'),
                                      'source': c.get('source', 'system' if not c.get('owned') else 'download'),
                                      'verified_by': c.get('verified_by')} for c in manifest.get('components', [])]}
        elif args.cmd == 'uninstall':
            result = uninstall(layout, registry, remove_history=args.remove_history, confirm=args.confirm,
                               dry_run=args.dry_run)
            for key in ('removed', 'kept', 'skipped', 'failed'):
                for line in result[key]:
                    log(f'{key.upper():8} {line}')
            result['ok'] = not result['failed']
        elif args.cmd == 'hostcheck':
            result = hostcheck(layout, registry)
            for line in result['lines']:
                log(line)
            log('\nAll checks passed.' if result['ok'] else '\nSomething is wrong; the FAIL lines above say what.')
        elif args.cmd == 'diagnose':
            extra = bmdfs.read_json(Path(args.extra_json)) if args.extra_json else None
            result = diagnose(layout, registry, extra if isinstance(extra, dict) else None)
            log('\nEverything checked out.' if result['ok'] else '\nSome checks failed; the FAIL lines above say what.')
        else:
            result = verify(layout, registry)
            for problem in result['problems']:
                log('PROBLEM: ' + problem)
        outcome.update(ok=bool(result.get('ok')), code=0 if result.get('ok') else 1, category=None, result=result)
        if args.json_report:
            Path(args.json_report).write_text(json.dumps(result, indent=2), encoding='utf-8')
        return outcome['code']
    except InstallError as exc:
        log('ERROR: ' + str(exc))
        outcome.update(ok=False, code=2, category=exc.category, message=str(exc))
        return 2
    except Exception as exc:            # anything unexpected: say so plainly and keep the details in the log
        log(f'ERROR: Setup hit an unexpected problem ({type(exc).__name__}: {exc}). '
            'Your previous installation was left or restored as it was.')
        log(traceback.format_exc())
        outcome.update(ok=False, code=1, category='failed',
                       message=f'{type(exc).__name__}: {exc}')
        return 1
    finally:
        if args.result_file:
            try:
                Path(args.result_file).write_text(json.dumps(outcome, indent=2), encoding='utf-8')
            except OSError:
                pass


if __name__ == '__main__':
    sys.exit(main())
