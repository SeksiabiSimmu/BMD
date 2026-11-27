#!/usr/bin/env python3
"""Pin exact versions and SHA-256 values in tools.lock.json (needs internet; run on a release machine).

The public release gate (scripts/build_release.py --require-pinned) and the offline installer both
need a pinned lock. A hash is written to the lock only after the real download has been checked
against something the publisher published separately from the download itself:

  yt-dlp    the SHA2-256SUMS file of the same release
  deno      the .sha256sum file of the same release
  ffmpeg    the .sha256 file on gyan.dev, else the one in the GitHub release
  python    the Sigstore bundle next to the archive (scripts/sigstore_verify.py)

Entries marked 'latest' are resolved to the release GitHub's latest-release link points to; the tag
is read from that link's redirect, because the GitHub API allows only 60 anonymous requests per
hour. Python's version is never changed here: the helper is tested against the version in the lock.

Verified downloads are kept in the release cache (as scripts/build_release.py does), so the build
does not download them again.

  python scripts/refresh_tool_lock.py                     # rewrite tools.lock.json
  python scripts/refresh_tool_lock.py --check             # do the pinned URLs still serve the pinned bytes?
  python scripts/refresh_tool_lock.py --lock other.json   # any lock file
"""
import argparse
import hashlib
import json
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'app' / 'installer'))
import build_release as br  # noqa: E402
import installer_core as ic  # noqa: E402
import sigstore_verify as sv  # noqa: E402

USER_AGENT = 'BraveMusicDownloader-LockRefresh/' + ic.VERSION
FFMPEG_VERSION_URL = 'https://www.gyan.dev/ffmpeg/builds/release-version'
GITHUB_RELEASES = {          # component -> (repository, asset asked for in the latest-release link)
    'yt-dlp': ('yt-dlp/yt-dlp', 'yt-dlp.exe'),
    'deno': ('denoland/deno', 'deno-x86_64-pc-windows-msvc.zip'),
}
REDIRECTS = (301, 302, 303, 307, 308)


class LockError(Exception):
    pass


def get(url, opener=urllib.request.urlopen, timeout=60):
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with opener(request, timeout=timeout) as response:
        return response.read()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def latest_tag(repo, asset, redirect=None):
    """The tag of the release GitHub's 'latest release' link points to.

    https://github.com/<repo>/releases/latest/download/<asset> answers 302 and names the release in
    the Location header: one HEAD request, no API quota."""
    url = f'https://github.com/{repo}/releases/latest/download/{asset}'
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT}, method='HEAD')
    try:
        with (redirect or urllib.request.build_opener(NoRedirect)).open(request, timeout=60) as response:
            raise LockError(f'github.com did not redirect {url} (HTTP {response.status}): '
                            f'is "{asset}" a release asset of {repo}?')
    except urllib.error.HTTPError as exc:
        location = exc.headers.get('Location') if exc.headers else None
        if exc.code not in REDIRECTS or not location:
            raise LockError(f'github.com did not redirect {url} (HTTP {exc.code}): '
                            'cannot tell which release is the latest') from None
    location = urllib.parse.urljoin(url, location)
    match = re.fullmatch(r'https://github\.com/([^/]+/[^/]+)/releases/download/([^/]+)/(.+)', location)
    if not match or match.group(1).lower() != repo.lower() or match.group(3) != asset:
        raise LockError(f'{url} redirects to {location}, which is not a release download of {asset}')
    return match.group(2)


def optional_text(url, opener):
    """The text at `url`, or None when the publisher has no such file."""
    try:
        return get(url, opener).decode('utf-8')
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def newest(entry, opener, redirect):
    """(version, url, publisher checksum or None, checksum source or None) for the newest release."""
    name = entry['name']
    if name in GITHUB_RELEASES:
        repo, asset = GITHUB_RELEASES[name]
        tag = latest_tag(repo, asset, redirect)
        base = f'https://github.com/{repo}/releases/download/{tag}/'
        sources = ['SHA2-256SUMS'] if name == 'yt-dlp' else [asset + '.sha256sum', asset + '.sha256']
        version = tag.lstrip('v') if name == 'deno' else tag
    elif name == 'ffmpeg':
        version = get(FFMPEG_VERSION_URL, opener).decode('utf-8').strip()
        if not re.fullmatch(r'\d+(\.\d+)+', version):
            raise LockError(f'ffmpeg: {FFMPEG_VERSION_URL} answered {version!r} instead of a version number')
        asset = f'ffmpeg-{version}-essentials_build.zip'
        base = f'https://github.com/GyanD/codexffmpeg/releases/download/{version}/'
        sources = ['https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip.sha256', base + asset + '.sha256']
    else:
        raise LockError(f'{name}: this script does not know where the newest version is published')
    for source in sources:
        text = optional_text(source if source.startswith('https://') else base + source, opener)
        if text:
            try:
                return version, base + asset, ic.parse_checksum_text(text, asset), source
            except ic.InstallError as exc:
                raise LockError(f'{name}: {source}: {exc}') from None
    return version, base + asset, None, None


def pin_python(entry, cache, opener, sigstore_python):
    """(sha256, evidence) of the Python archive: downloaded, then confirmed through Sigstore."""
    url = entry['url']
    with tempfile.TemporaryDirectory(prefix='bmd-lock-') as folder:
        archive = Path(folder) / url.rsplit('/', 1)[-1]
        try:
            ic.download_verified(url, archive, None, opener=opener)
        except ic.InstallError as exc:
            raise LockError(f'python: {exc}') from None
        try:
            report = sv.verify(entry['version'], archive, fetch_bundle(url, folder, opener), python=sigstore_python)
        except (sv.VerifyError, OSError) as exc:
            raise LockError(f'python: the download was not confirmed as the official build: {exc}') from None
        digest = ic.sha256_file(archive)
        shutil.copyfile(archive, br.cache_file('python', digest, cache))
    evidence = (f"Sigstore bundle; signer {report['identity']} through {report['issuer']}, Rekor log index "
                f"{report['log_index']}, as listed at https://www.python.org/downloads/metadata/sigstore")
    return digest, evidence


def fetch_bundle(url, folder, opener):
    bundle_url = url + '.sigstore'
    target = Path(folder) / bundle_url.rsplit('/', 1)[-1]
    try:
        target.write_bytes(get(bundle_url, opener))
    except urllib.error.HTTPError as exc:
        raise sv.VerifyError(f'there is no Sigstore bundle at {bundle_url} (HTTP {exc.code})') from None
    return target


def refresh(lock, opener=urllib.request.urlopen, redirect=None, cache=None, sigstore_python=None):
    """The lock with every component pinned to the newest release; Python keeps its version.

    `opener(request, timeout)` downloads; `redirect` (default: urllib without redirect following)
    answers the HEAD request that finds GitHub's latest release."""
    cache = Path(cache or ROOT / '.release-cache')
    cache.mkdir(parents=True, exist_ok=True)
    out, evidence = [], {}
    for entry in lock['components']:
        name = entry['name']
        print(f'Resolving {name}…', flush=True)
        if name == 'python':
            version, url = entry['version'], entry['url']
            digest, evidence[name] = pin_python(entry, cache, opener, sigstore_python)
        else:
            version, url, expected, source = newest(entry, opener, redirect)
            if expected is None:
                raise LockError(f'{name}: {url} has no publisher checksum to compare the download with; '
                                'refusing to pin a hash that was not cross-checked')
            try:
                digest = ic.download_verified(url, br.cache_file(name, expected, cache), expected,
                                              opener=opener, attempts=3, delay=2.0)
            except ic.InstallError as exc:
                raise LockError(f'{name}: {exc}') from None
            evidence[name] = f'publisher checksum {source}'
        if digest != ic.sha256_file(br.cache_file(name, digest, cache)):
            raise LockError(f'{name}: the kept download changed under the refresh')
        updated = {k: v for k, v in entry.items() if k not in ('checksum', 'authenticode')}
        updated.update(version=version, url=url, sha256=digest)
        out.append(updated)
    note = ('Every component is pinned: setup downloads exactly these versions and refuses a file with another '
            'SHA-256. How each value was cross-checked is listed under "pinned" (scripts/refresh_tool_lock.py). '
            'To take in newer tools run that script again, then rebuild.')
    return {**lock, 'note': note, 'components': out,
            'pinned': {'date': time.strftime('%Y-%m-%d'), 'by': 'scripts/refresh_tool_lock.py', 'evidence': evidence}}


def check(lock, opener=urllib.request.urlopen):
    """Problems that make the lock unusable as a pinned lock; every pinned URL is downloaded again."""
    bad = []
    with tempfile.TemporaryDirectory(prefix='bmd-lock-check-') as folder:
        for entry in lock['components']:
            name = entry['name']
            if not entry.get('sha256'):
                bad.append(f'{name}: not pinned')
                continue
            try:
                actual = ic.download_verified(entry['url'], Path(folder) / name, None, opener=opener)
            except ic.InstallError as exc:
                bad.append(f'{name}: {exc}')
                continue
            if actual != entry['sha256']:
                bad.append(f'{name}: {entry["url"]} now serves a file with SHA-256 {actual}, '
                           f'the lock pins {entry["sha256"]}')
            else:
                print(f'{name}: ok ({entry.get("version", "")})')
    return bad


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--check', action='store_true', help='download the pinned files again and compare')
    parser.add_argument('--lock', default=str(ROOT / 'tools.lock.json'))
    parser.add_argument('--cache', default=str(ROOT / '.release-cache'), help='where verified downloads are kept')
    args = parser.parse_args(argv)
    path = Path(args.lock)
    lock = json.loads(path.read_text(encoding='utf-8'))
    try:
        if args.check:
            bad = check(lock)
            for line in bad:
                print(line)
            return 1 if bad else 0
        refreshed = refresh(lock, cache=args.cache)
        path.write_text(json.dumps(refreshed, indent=2) + '\n', encoding='utf-8')
        for entry in refreshed['components']:
            print(f"  {entry['name']:8} {entry['version']:14} sha256 {entry['sha256']}")
        print('Wrote', path)
        return 0
    except (LockError, OSError) as exc:
        print(f'LOCK REFRESH FAILED: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
