#!/usr/bin/env python3
"""Static heuristics for the Brave Music Downloader browser extension.

This is the lightweight, dependency-free gate the QA report refers to as
``check_extension.py app/extension``. It does not run the browser; it catches the
extension-specific mistakes that break a load or a native round trip:

  * manifest.json is valid JSON and a Manifest V3 document
  * the listed service_worker / content_scripts / icons / popup / options_page files exist
  * every <script src=> and <link href=> referenced from the HTML pages exists in the folder
  * the permissions set is exactly the documented one (so a permission creep is caught)
  * host_permissions cover the YouTube hosts the content scripts match
  * the commands and actions reference files that exist
  * no version number is written into an HTML page (the pages read it from the manifest;
    a literal went stale with every release before 2.5.0)

Exit code is non-zero if any finding is reported. Run it from the repo root:

    python scripts/check_extension.py app/extension
"""
import argparse
import json
import re
import sys
from pathlib import Path

# The permission set the release gate pins; a change here is a review event, not a fix.
# 2.4.0 audit: "tabs" was removed — the extension only reads url/title of the active tab,
# and host_permissions (YouTube only) already expose those fields for YouTube tabs, so the
# sensitive "tabs" permission (all-sites url/title access) was unnecessary.
EXPECTED_PERMISSIONS = {
    'storage', 'contextMenus', 'notifications', 'alarms', 'nativeMessaging',
}
# The YouTube hosts the content scripts and host permissions must keep in lockstep.
YOUTUBE_HOSTS = {
    'https://www.youtube.com/*', 'https://music.youtube.com/*',
    'https://m.youtube.com/*', 'https://youtube.com/*', 'https://youtu.be/*',
}
# A version in the text of a page, as in "Studio v2.4" or "version 2.4" (the number is a group).
VERSION_LITERAL = re.compile(r'(?<![A-Za-z0-9])(?:[vV](\d+\.\d+(?:\.\d+)?)|[Vv]ersion\s+(\d+\.\d+(?:\.\d+)?))'
                             r'(?![A-Za-z0-9])')


class Finding(Exception):
    pass


def check_extension(folder: Path) -> list:
    """Return a list of human-readable findings (empty means clean)."""
    findings = []
    folder = folder.resolve()

    manifest_path = folder / 'manifest.json'
    if not manifest_path.exists():
        return [f'manifest.json missing at {manifest_path}']
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        return [f'manifest.json is not valid JSON: {exc}']

    if manifest.get('manifest_version') != 3:
        findings.append(f'manifest_version is {manifest.get("manifest_version")}, expected 3')

    # Permissions: must match the documented set exactly.
    perms = set(manifest.get('permissions', []))
    if perms != EXPECTED_PERMISSIONS:
        missing = sorted(EXPECTED_PERMISSIONS - perms)
        extra = sorted(perms - EXPECTED_PERMISSIONS)
        if missing:
            findings.append('permissions missing: ' + ', '.join(missing))
        if extra:
            findings.append('permissions not in the documented set: ' + ', '.join(extra))

    def must_exist(rel, label):
        if not rel:
            return
        path = folder / rel
        if not path.exists():
            findings.append(f'{label} references missing file: {rel}')

    # Background service worker.
    background = manifest.get('background', {})
    if isinstance(background, dict):
        must_exist(background.get('service_worker'), 'background.service_worker')

    # Action popup / default icons.
    action = manifest.get('action', {})
    if isinstance(action, dict):
        must_exist(action.get('default_popup'), 'action.default_popup')
        default_icon = action.get('default_icon')
        if isinstance(default_icon, str):
            must_exist(default_icon, 'action.default_icon')
        elif isinstance(default_icon, dict):
            for size, rel in default_icon.items():
                must_exist(rel, f'action.default_icon[{size}]')

    # options_page / options_ui.
    must_exist(manifest.get('options_page'), 'options_page')
    options_ui = manifest.get('options_ui', {})
    if isinstance(options_ui, dict):
        must_exist(options_ui.get('page'), 'options_ui.page')

    # Icons referenced by name.
    icons = manifest.get('icons', {})
    if isinstance(icons, dict):
        for size, rel in icons.items():
            must_exist(rel, f'icons[{size}]')

    # Content scripts and their matches.
    for i, entry in enumerate(manifest.get('content_scripts', []) or []):
        for j, js in enumerate(entry.get('js', []) or []):
            must_exist(js, f'content_scripts[{i}].js[{j}]')
        for j, css in enumerate(entry.get('css', []) or []):
            must_exist(css, f'content_scripts[{i}].css[{j}]')

    # Host permissions must keep the YouTube set so the content scripts can run.
    host_perms = set(manifest.get('host_permissions', []) or [])
    missing_hosts = sorted(YOUTUBE_HOSTS - host_perms)
    if missing_hosts:
        findings.append('host_permissions missing YouTube hosts: ' + ', '.join(missing_hosts))

    # Command shortcut pages do not reference files, but their keys are validated by Chrome.
    return findings


def check_html_refs(folder: Path) -> list:
    """Scan the extension's HTML pages for <script src> and <link href> that point at files
    that do not exist in the folder (a typo'd src is the most common cause of a blank popup)."""
    findings = []
    for page in sorted(folder.glob('*.html')):
        text = page.read_text(encoding='utf-8')
        for m in re.finditer(r'<script[^>]*\bsrc=["\']([^"\']+)["\']', text):
            ref = m.group(1)
            if not ref or ref.startswith(('http://', 'https://', 'chrome-extension://')):
                continue
            if not (folder / ref).exists():
                findings.append(f'{page.name}: <script src> missing file: {ref}')
        for m in re.finditer(r'<link[^>]*\bhref=["\']([^"\']+)["\']', text):
            ref = m.group(1)
            if not ref or ref.startswith(('http://', 'https://', 'chrome-extension://', '#')):
                continue
            if not (folder / ref).exists():
                findings.append(f'{page.name}: <link href> missing file: {ref}')
    return findings


def check_version_literals(folder: Path) -> list:
    """A version number written into an HTML page. The pages show the version of the installed
    package by reading chrome.runtime.getManifest(); a literal in the page goes stale with every
    release (the 2.3.0 pages still said v2.0)."""
    findings = []
    for page in sorted(folder.glob('*.html')):
        for match in VERSION_LITERAL.finditer(page.read_text(encoding='utf-8')):
            findings.append(f'{page.name}: the version {match.group(1) or match.group(2)} is written into the page; '
                            'the pages read it from chrome.runtime.getManifest()')
    return findings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', help='path to the extension folder, e.g. app/extension')
    args = parser.parse_args(argv)
    folder = Path(args.folder)
    if not folder.is_dir():
        print(f'check_extension: not a directory: {folder}', file=sys.stderr)
        return 2
    findings = check_extension(folder) + check_html_refs(folder) + check_version_literals(folder)
    if findings:
        for line in findings:
            print(f'check_extension: {line}')
        return 1
    print(f'check_extension: no findings ({folder})')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
