#!/usr/bin/env python3
"""Assemble the Brave Music Downloader release from this source tree.

  python scripts/build_release.py --out dist                      # both installers
  python scripts/build_release.py --out dist --require-pinned     # public release gate
  python scripts/build_release.py --out dist --online-only        # no tool downloads

Every installer is one Windows program: the native launcher (app/installer/launcher/, built here
with Zig) followed by the files to install, in the layout of app/installer/payload_container.py.

  BraveMusicDownloader-Setup.exe           Setup.ps1 and Application.zip; the tools are downloaded while the user installs
  BraveMusicDownloader-Setup-offline.exe   the same, with Python, yt-dlp, deno and ffmpeg on board (needs a pinned tools.lock.json)

The folder also gets Application.zip with a copy of Setup.ps1 (run it next to the archive to install
without the exe), BraveMusicDownloader-extension-<version>.zip (the extension alone, manifest.json at
the root of the archive), SHA256SUMS.txt and release.json. The same sources, Zig version and lock
always give the same bytes. Nothing here signs the files: see docs/RELEASE.md.

Exit codes: 0 built, 1 the build failed, 2 a release gate refused the tree (the reason is printed).
"""
import argparse
import ast
import base64
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'app' / 'installer'))
from check_extension import (check_extension as _check_extension, check_html_refs as _check_html_refs,  # noqa: E402
                            check_version_literals as _check_version_literals)
import build_launcher  # noqa: E402
import installer_core as ic  # noqa: E402
import payload_container  # noqa: E402
from pe_tools import pe_info, program_problems  # noqa: E402

PAYLOAD = [('app/extension', 'extension'), ('app/helper', 'helper'), ('app/installer', 'installer'),
           ('app/installer/launcher/setup.ico', 'bridge/setup.ico'),
           ('app/bridge/NativeLauncher.cs', 'bridge/NativeLauncher.cs'), ('app/README.md', 'README.md'),
           ('app/Uninstall.ps1', 'Uninstall.ps1'), ('tools.lock.json', 'tools.lock.json')]
# The setup program's own sources are compiled into the exe; they are not installed.
NOT_INSTALLED = ('app/installer/launcher/',)
# Windows PowerShell 5.1 reads a script without byte-order mark as ANSI text: these must be ASCII.
SCRIPTS = ('Setup.ps1', 'app/Uninstall.ps1')
FORBIDDEN_SUFFIXES = {'.exe', '.dll', '.pyd', '.pyc', '.so', '.msi', '.obj', '.o', '.lib', '.res', '.pdb'}
FORBIDDEN_DIRS = {'__pycache__', '.git', 'node_modules', '.pytest_cache', '.zig-cache'}
STALE_PHRASES = ('Install Native Host', 'Install Tools.ps1', 'ThreadingHTTPServer', 'Bearer ',
                 'Build-Exe.cmd', 'SetupStub.cs', 'Install-Dev.cmd', 'installer_launcher.c')
# 2.4.0 audit: "tabs" removed — host_permissions (YouTube only) already expose the
# active tab's url/title for YouTube tabs, so the sensitive "tabs" permission was unnecessary.
EXPECTED_PERMISSIONS = {'storage', 'contextMenus', 'notifications', 'alarms', 'nativeMessaging'}
TEXT_SUFFIXES = {'.js', '.html', '.css', '.json', '.md', '.ps1', '.py', '.cs', '.txt'}

ONLINE = 'BraveMusicDownloader-Setup.exe'
OFFLINE = 'BraveMusicDownloader-Setup-offline.exe'
EXTENSION_ZIP = 'BraveMusicDownloader-extension-{version}.zip'
EXTENSION_PREFIX = 'extension/'                  # where the extension sits inside Application.zip
BUNDLED = ('python', 'yt-dlp', 'deno', 'ffmpeg')    # by lock name; the launcher unpacks python before PowerShell starts
# Files of earlier release layouts that must not be handed out with this one.
STALE_OUTPUTS = ('Build-Exe.cmd', 'Install-Dev.cmd', 'SetupStub.cs', 'installer_launcher.generated.c')


class ReleaseError(Exception):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def extension_id(key_b64):
    digest = hashlib.sha256(base64.b64decode(key_b64)).digest()[:16]
    return ''.join('abcdefghijklmnop'[b >> 4] + 'abcdefghijklmnop'[b & 15] for b in digest)


def collect(source):
    """(archive name, path) for every payload file; refuses build debris and binaries."""
    entries = []
    for src, dest in PAYLOAD:
        base = source / src
        if not base.exists():
            raise ReleaseError(f'Missing payload item: {src}')
        listed = base.is_file()
        files = [base] if listed else sorted(p for p in base.rglob('*') if p.is_file())
        for path in files:
            parts = set(path.relative_to(source).parts)
            if parts & FORBIDDEN_DIRS:
                raise ReleaseError(f'Build debris in payload: {path.relative_to(source)} '
                                   '(delete the __pycache__ folders, or run the tests with PYTHONDONTWRITEBYTECODE=1)')
            if path.suffix.lower() in FORBIDDEN_SUFFIXES:
                raise ReleaseError(f'Binary files must not be bundled: {path.relative_to(source)}')
            if not listed and path.relative_to(source).as_posix().startswith(NOT_INSTALLED):
                continue
            name = dest if listed else f'{dest}/{path.relative_to(base).as_posix()}'
            entries.append((name, path))
    return sorted(entries)


def text_of(source, rel):
    """A text file of the tree. One that is missing or unreadable blocks the release instead of
    stopping the build with a traceback."""
    try:
        return (source / rel).read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as exc:
        raise ReleaseError(f'{rel}: cannot be read: {exc}') from None


def json_of(source, rel):
    try:
        return json.loads(text_of(source, rel))
    except ValueError as exc:
        raise ReleaseError(f'{rel}: is not valid JSON: {exc}') from None


def check_versions(source):
    version = text_of(source, 'VERSION').strip()
    found = {}
    found['extension manifest'] = json_of(source, 'app/extension/manifest.json').get('version')
    for label, rel, pattern in [
            ('installer_core', 'app/installer/installer_core.py', r"^VERSION = '([^']+)'"),
            ('helper', 'app/helper/helper.py', r"^VERSION = '([^']+)'"),
            ('Setup.ps1', 'Setup.ps1', r"\$script:Version = '(\d+\.\d+\.\d+)'"),
            ('setup program', 'app/installer/launcher/setup_version.h', r'#define SETUP_VERSION_TEXT "([^"]+)"'),
            ('setup manifest', 'app/installer/launcher/setup.manifest',
             r'assemblyIdentity[^>]*version="(\d+\.\d+\.\d+)\.0"'),
            ('CHANGELOG', 'CHANGELOG.md', r'^## (\d+\.\d+\.\d+)'),
            ('README', 'app/README.md', r'(?:v|version )(\d+\.\d+\.\d+)')]:
        match = re.search(pattern, text_of(source, rel), re.M)
        found[label] = match.group(1) if match else None
    wrong = {k: v for k, v in found.items() if v != version}
    if wrong:
        raise ReleaseError(f'Version mismatch against VERSION={version}: {wrong}')
    return version


def check_identity(source):
    manifest = json_of(source, 'app/extension/manifest.json')
    derived = extension_id(manifest['key'])
    recorded = text_of(source, 'extension_id.txt').strip()
    in_setup = re.search(r"\$script:ExtensionId = '([a-p]{32})'", text_of(source, 'Setup.ps1'))
    if not (derived == recorded == (in_setup.group(1) if in_setup else None)):
        raise ReleaseError('Extension ID differs between manifest key, extension_id.txt and Setup.ps1')
    if set(manifest.get('permissions', [])) != EXPECTED_PERMISSIONS:
        raise ReleaseError('Extension permissions changed: ' + ', '.join(sorted(manifest.get('permissions', []))))
    if manifest.get('manifest_version') != 3:
        raise ReleaseError('Extension must be Manifest V3')
    return derived


def check_extension(source):
    """Static heuristics for the extension folder (manifest consistency, dangling file refs)."""
    folder = source / 'app/extension'
    findings = _check_extension(folder) + _check_html_refs(folder) + _check_version_literals(folder)
    if findings:
        raise ReleaseError('Extension static check failed:\n  ' + '\n  '.join(findings))


def check_lock(source, require_pinned):
    lock = json_of(source, 'tools.lock.json')
    names, summary = set(), {}
    for entry in lock['components']:
        name = entry['name']
        if name in names:
            raise ReleaseError('Duplicate component ' + name)
        names.add(name)
        if not str(entry['url']).startswith('https://') or not entry.get('provides'):
            raise ReleaseError(f'{name}: needs an https url and provided files')
        if entry.get('sha256'):
            summary[name] = 'pinned'
        elif entry.get('checksum'):
            summary[name] = 'publisher checksum'
            if not str(entry['checksum']['url']).startswith('https://'):
                raise ReleaseError(f'{name}: checksum url must be https')
        elif entry.get('authenticode'):
            summary[name] = 'code signature (checked by Setup.ps1)'
        else:
            raise ReleaseError(f'{name}: no pinned hash, publisher checksum or signature check')
        if require_pinned and summary[name] != 'pinned':
            raise ReleaseError(f'{name}: --require-pinned needs a sha256; run scripts/refresh_tool_lock.py first')
    return summary


def check_content(entries, source):
    for name, path in entries:
        text = None
        try:
            if path.suffix == '.py':
                ast.parse(path.read_text(encoding='utf-8'), filename=name)
            if path.suffix in TEXT_SUFFIXES:
                text = path.read_text(encoding='utf-8')
        except (SyntaxError, UnicodeDecodeError) as exc:
            raise ReleaseError(f'{name}: is not valid text: {exc}') from None
        if text is not None:
            for phrase in STALE_PHRASES:
                if phrase in text:
                    raise ReleaseError(f'{name}: contains stale text "{phrase}"')
        if path.suffix == '.js' and shutil.which('node'):
            run = subprocess.run(['node', '--check', str(path)], capture_output=True, text=True)
            if run.returncode:
                raise ReleaseError(f'{name}: JavaScript syntax error: {run.stderr[:300]}')
    for rel in SCRIPTS:
        if not (source / rel).read_bytes().isascii():
            raise ReleaseError(f'{rel}: must be ASCII only; Windows PowerShell 5.1 would read other characters wrong')


def extension_archive(entries):
    """The extension alone: [(archive name, path)] with manifest.json at the root of the archive,
    which is what an extension store takes and what a user unpacks to load it."""
    return [(name[len(EXTENSION_PREFIX):], path) for name, path in entries if name.startswith(EXTENSION_PREFIX)]


def extension_problems(data, entries, version):
    """Why the extension archive `data` is not the one `entries` = [(name, path)] describes."""
    problems = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            if bundle.namelist() != [name for name, _ in entries]:
                problems.append(f'the archive holds {bundle.namelist()}')
            for name, path in entries:
                try:
                    if bundle.read(name) != path.read_bytes():
                        problems.append(f'{name}: the stored bytes differ from the tree')
                except KeyError:
                    pass                                    # a missing file is reported with the list above
                except (zipfile.BadZipFile, zlib.error) as exc:   # damaged data: zipfile raises either
                    problems.append(f'{name}: damaged ({exc})')
            try:
                found = json.loads(bundle.read('manifest.json')).get('version')
            except (KeyError, ValueError):
                found = None
            if found != version:
                problems.append(f'manifest.json says version {found}')
    except zipfile.BadZipFile as exc:
        problems.append(str(exc))
    return problems


def build_zip(entries, destination):
    """An archive of `entries`: the same entries always give the same bytes."""
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for name, path in entries:
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            bundle.writestr(info, path.read_bytes())


# ---- the installers --------------------------------------------------------------------------

def cache_file(name, expected, cache):
    """Where a verified download of component `name` with hash `expected` is kept. scripts/refresh_tool_lock.py
    fills the same cache, so a build right after a lock refresh downloads nothing."""
    return Path(cache) / f'{name}-{str(expected).lower()[:16]}.bin'


def pinned_bytes(entry, cache, opener=ic.default_opener):
    """A pinned component's download, kept in `cache` under its hash once it has been verified.
    The download is the engine's own (resume, retries, size limit), so the build checks what the
    installer would check."""
    expected = str(entry['sha256']).lower()
    kept = cache_file(entry['name'], expected, cache)
    try:
        ic.download_verified(entry['url'], kept, expected, opener=opener, attempts=3, delay=2.0)
    except ic.InstallError as exc:
        raise ReleaseError(f"{entry['name']}: {exc}") from None
    data = kept.read_bytes()
    if sha256(data) != expected:
        raise ReleaseError(f"{entry['name']}: the kept download does not match the pinned SHA-256")
    return data


def bundled(lock, cache, opener=ic.default_opener):
    """The offline installer's tool archives as [(entry name, bytes, unpacked by the launcher)]."""
    by_name = {entry['name']: entry for entry in lock.get('components', [])}
    missing = [name for name in BUNDLED if name not in by_name]
    if missing:
        raise ReleaseError('tools.lock.json has no ' + ', '.join(missing)
                           + ': the offline installer needs ' + ', '.join(BUNDLED))
    unpinned = [name for name in BUNDLED if not by_name[name].get('sha256')]
    if unpinned:
        raise ReleaseError('the offline installer needs a pinned lock (' + ', '.join(unpinned)
                           + ' is not pinned): run scripts/refresh_tool_lock.py first, or build with --online-only')
    result = []
    for name in BUNDLED:
        entry = by_name[name]
        print(f"Fetching {name} {entry.get('version', '')}…", flush=True)
        result.append((name, pinned_bytes(entry, cache, opener), name == 'python'))
    return result


def installer_problems(data, version, expected):
    """Why the installer file `data` is not the one `expected` = [(name, bytes, unpacked by the
    launcher)] describes. Reads the file back the way the setup program does."""
    try:
        info = pe_info(data)
    except ValueError as exc:
        return [str(exc)]
    bad = program_problems(info, version)
    if info['certificate']['size']:
        bad.append('the file is signed; a fresh build is not (signing is the last step)')
    try:
        container = payload_container.parse(data)
    except payload_container.ContainerError as exc:
        return bad + [str(exc)]
    if container['pe_size'] != info['pe_size']:
        bad.append(f"the container starts at {container['pe_size']}, the program ends at {info['pe_size']}")
    found = [(entry['name'], entry['bootstrap']) for entry in container['entries']]
    if found != [(name, unpacked) for name, _, unpacked in expected]:
        bad.append(f'the container holds {found}')
    for entry, (name, blob, _) in zip(container['entries'], expected):
        start = entry['offset']
        if data[start:start + entry['size']] != blob:
            bad.append(f'{name}: the stored bytes differ from the built ones')
    return bad


def build(source, out, facts, lock, zig=None, online_only=False, cache=None, opener=ic.default_opener):
    """Write the release into `out`. Returns what is also written as release.json."""
    out.mkdir(parents=True, exist_ok=True)
    for name in STALE_OUTPUTS:
        (out / name).unlink(missing_ok=True)
    if online_only:
        (out / OFFLINE).unlink(missing_ok=True)
    extension_zip = out / EXTENSION_ZIP.format(version=facts['version'])
    for stale in out.glob(EXTENSION_ZIP.format(version='*')):      # the extension of another version
        if stale.name != extension_zip.name:
            stale.unlink()
    entries = collect(source)
    payload = out / 'Application.zip'
    build_zip(entries, payload)
    extension = extension_archive(entries)
    build_zip(extension, extension_zip)
    problems = extension_problems(extension_zip.read_bytes(), extension, facts['version'])
    if problems:
        raise ReleaseError(f'{extension_zip.name} is not a usable extension archive:\n  ' + '\n  '.join(problems))
    setup_bytes = (source / 'Setup.ps1').read_bytes()
    (out / 'Setup.ps1').write_bytes(setup_bytes)
    common = [('Setup.ps1', setup_bytes, True), ('Application.zip', payload.read_bytes(), True)]
    kinds = [(ONLINE, 'online', common)]
    if not online_only:
        kinds.append((OFFLINE, 'offline', common + bundled(lock, cache or source / '.release-cache', opener)))

    with tempfile.TemporaryDirectory(prefix='.build-', dir=out) as folder:
        image = Path(folder) / 'launcher.exe'
        launcher_sha, launcher_size = build_launcher.build(zig, image, work=Path(folder) / 'work',
                                                            folder=source / 'app' / 'installer' / 'launcher')
        image_bytes = image.read_bytes()
        zig_version = build_launcher.run([build_launcher.find_zig(zig), 'version']).stdout.strip()

    installers, files = {}, {}
    for name, kind, expected in kinds:
        data = payload_container.build(image_bytes, expected)
        problems = installer_problems(data, facts['version'], expected)
        if problems:
            raise ReleaseError(f'{name} is not a usable installer:\n  ' + '\n  '.join(problems))
        (out / name).write_bytes(data)
        installers[name] = {
            'kind': kind, 'bytes': len(data), 'sha256': sha256(data), 'signed': False,
            'entries': [{'name': entry, 'bytes': len(blob), 'sha256': sha256(blob), 'unpacked_by_launcher': unpacked}
                        for entry, blob, unpacked in expected]}
        files[name] = sha256(data)
    for path in (payload, extension_zip, out / 'Setup.ps1'):
        files[path.name] = sha256(path.read_bytes())
    (out / 'SHA256SUMS.txt').write_text(''.join(f'{files[name]}  {name}\n' for name in sorted(files)), encoding='utf-8')
    release = {'product': 'Brave Music Downloader', **facts, 'payload_files': len(entries), 'zig': zig_version,
               'launcher': {'bytes': launcher_size, 'sha256': launcher_sha},
               'extension': {'file': extension_zip.name, 'bytes': extension_zip.stat().st_size,
                             'sha256': files[extension_zip.name], 'files': len(extension)},
               'installers': installers, 'files': files, 'signed': False,
               'signature_note': 'Not signed. Sign the finished installers with signtool (docs/RELEASE.md); '
                                 'the container stays valid because it is found through the PE header.'}
    (out / 'release.json').write_text(json.dumps(release, indent=2) + '\n', encoding='utf-8')
    return release


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--source', default=str(ROOT))
    parser.add_argument('--out', default=str(ROOT / 'dist'))
    parser.add_argument('--require-pinned', action='store_true',
                        help='public release gate: every tool needs a pinned SHA-256')
    parser.add_argument('--online-only', action='store_true', help='skip the offline installer (no downloads)')
    parser.add_argument('--zig', help='the zig executable (default: $ZIG, or zig on the PATH)')
    parser.add_argument('--cache', help=f'where verified tool downloads are kept (default: <source>/.release-cache)')
    args = parser.parse_args(argv)
    source, out = Path(args.source).resolve(), Path(args.out).resolve()
    try:
        version = check_versions(source)
        ext_id = check_identity(source)
        check_extension(source)
        lock_summary = check_lock(source, args.require_pinned)
        entries = collect(source)
        check_content(entries, source)
    except ReleaseError as exc:
        print('RELEASE BLOCKED: ' + str(exc), file=sys.stderr)
        return 2
    facts = {'version': version, 'extension_id': ext_id, 'tools': lock_summary,
             'release_gate': 'pinned' if args.require_pinned else 'development (tools not fully pinned)'}
    try:
        release = build(source, out, facts, json.loads((source / 'tools.lock.json').read_text(encoding='utf-8')),
                        zig=args.zig, online_only=args.online_only, cache=Path(args.cache) if args.cache else None)
    except (ReleaseError, build_launcher.BuildError) as exc:
        print('RELEASE FAILED: ' + str(exc), file=sys.stderr)
        return 1
    for name, info in release['installers'].items():
        print(f"{out / name}  {info['bytes']} bytes  sha256 {info['sha256']}")
    extension = release['extension']
    print(f"{out / extension['file']}  {extension['bytes']} bytes  {extension['files']} files  "
          f"sha256 {extension['sha256']}")
    print(f'{out / "SHA256SUMS.txt"}  {len(release["files"])} files  release gate: {facts["release_gate"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
