#!/usr/bin/env python3
"""Verify a python.org release archive against the Sigstore bundle published next to it.

    python scripts/sigstore_verify.py <version> <cache-folder> [--offline] [--python <interpreter>]

scripts/refresh_tool_lock.py runs this before it pins a Python archive. python.org publishes no
checksum file, so the evidence that the bytes are the official build is a signature made with a
short-lived certificate that only the release manager's login can obtain, recorded in the public
Rekor log. Who that is for each Python version is published at
https://www.python.org/downloads/metadata/sigstore ("Verification additionally requires prior
knowledge of the identity of the signer"), so the expected signer is looked up there, not read
from the bundle.

Needs the `sigstore` package (pip install sigstore) in the interpreter that runs the check and,
without --offline, access to the public Sigstore services.
"""
import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

USER_AGENT = 'BraveMusicDownloader-LockRefresh'
# https://www.python.org/downloads/metadata/sigstore (read 2026-11-27): version line -> (identity, issuer)
SIGNERS = {
    '3.7': ('nad@python.org', 'https://github.com/login/oauth'),
    '3.8': ('lukasz@langa.pl', 'https://github.com/login/oauth'),
    '3.9': ('lukasz@langa.pl', 'https://github.com/login/oauth'),
    '3.10': ('pablogsal@python.org', 'https://accounts.google.com'),
    '3.11': ('pablogsal@python.org', 'https://accounts.google.com'),
    '3.12': ('thomas@python.org', 'https://accounts.google.com'),
    '3.13': ('thomas@python.org', 'https://accounts.google.com'),
    '3.14': ('hugo@python.org', 'https://github.com/login/oauth'),
    '3.15': ('hugo@python.org', 'https://github.com/login/oauth'),
    '3.16': ('savannah@python.org', 'https://github.com/login/oauth'),
    '3.17': ('savannah@python.org', 'https://github.com/login/oauth'),
}


class VerifyError(Exception):
    pass


def signer(version):
    """(identity, issuer) python.org names as the release manager of `version`'s line ('3.12')."""
    line = '.'.join(version.split('.')[:2])
    if line not in SIGNERS:
        raise VerifyError(f'Python {line}: no release manager is listed here; take the identity and issuer from '
                          'https://www.python.org/downloads/metadata/sigstore and add them to SIGNERS.')
    return SIGNERS[line]


def fetch(url, target):
    if not target.is_file():
        request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        with urllib.request.urlopen(request, timeout=300) as response, target.open('wb') as out:
            while chunk := response.read(1 << 20):
                out.write(chunk)
    return target


def log_entry(bundle):
    """(Rekor log index, log id) recorded in the bundle, for the record."""
    entries = bundle.get('verificationMaterial', {}).get('tlogEntries') or []
    if not entries:
        return None, None
    return entries[0].get('logIndex'), entries[0].get('logId')


def verify(version, archive, bundle, offline=False, python=None):
    """Raise VerifyError unless the Sigstore client confirms `archive` against `bundle`.

    Checks the signature, the certificate chain to Sigstore's root, that the certificate was issued
    to the release manager python.org names, and the entry in the Rekor log. Returns what was seen."""
    identity, issuer = signer(version)
    command = [python or sys.executable, '-m', 'sigstore', 'verify', 'identity', '--bundle', str(bundle),
               '--cert-identity', identity, '--cert-oidc-issuer', issuer]
    if offline:
        command.append('--offline')
    run = subprocess.run([*command, str(archive)], capture_output=True, text=True)
    output = (run.stdout + run.stderr).strip()
    if run.returncode:
        hint = ' (the sigstore package is not installed for this interpreter: pip install sigstore)' \
            if 'No module named' in output else ''
        raise VerifyError(f'Sigstore did not confirm {archive.name}:{hint}\n{output[-800:]}')
    index, log_id = log_entry(json.loads(Path(bundle).read_text(encoding='utf-8')))
    return {'identity': identity, 'issuer': issuer, 'log_index': index, 'log_id': log_id, 'output': output}


def verify_url(url, cache, offline=False, python=None):
    """verify() for the archive at `url`, whose bundle is published next to it; both are kept in `cache`."""
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    name = url.rsplit('/', 1)[-1]
    version = name.split('-')[1]
    archive = fetch(url, cache / name)
    with tempfile.TemporaryDirectory(prefix='bmd-sigstore-') as folder:
        bundle = fetch(url + '.sigstore', Path(folder) / (name + '.sigstore'))
        report = verify(version, archive, bundle, offline=offline, python=python)
    report['sha256'] = hashlib.sha256(archive.read_bytes()).hexdigest()
    report['bytes'] = archive.stat().st_size
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('version', help="the Python version, for example 3.12.10")
    parser.add_argument('cache', type=Path, help='where the archive and its bundle are kept')
    parser.add_argument('--offline', action='store_true', help='do not refresh Sigstore\'s trust root')
    parser.add_argument('--python', help='the interpreter that has the sigstore package (default: this one)')
    args = parser.parse_args(argv)
    url = f'https://www.python.org/ftp/python/{args.version}/python-{args.version}-embed-amd64.zip'
    try:
        report = verify_url(url, args.cache, offline=args.offline, python=args.python)
    except (VerifyError, OSError) as exc:
        print(f'VERIFICATION FAILED: {exc}', file=sys.stderr)
        return 1
    print(f'python-{args.version}-embed-amd64.zip  {report["bytes"]} bytes  sha256 {report["sha256"]}')
    print(f'signed by {report["identity"]} through {report["issuer"]}; Rekor log index {report["log_index"]}')
    print(report['output'])
    return 0


if __name__ == '__main__':
    sys.exit(main())
