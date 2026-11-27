"""Locations used by the helper.

Layout of a real installation (all under the current Windows user):

    %LOCALAPPDATA%\\BraveMusicDownloader\\app       program files (owned, replaceable)
    %LOCALAPPDATA%\\BraveMusicDownloader\\cache     temporary cache (owned, removable)
    %APPDATA%\\BraveMusicDownloader                 queue database + settings (kept)
    %USERPROFILE%\\Music\\Brave Music Downloader    your music (never touched)

Environment overrides exist so tests and VMs can redirect every location.
"""
import os
from pathlib import Path


def _env(name):
    value = os.environ.get(name, '').strip()
    return Path(value) if value else None


APP_ROOT = _env('BMD_APP_ROOT') or Path(__file__).resolve().parents[1]
INSTALL_ROOT = _env('BMD_INSTALL_ROOT') or APP_ROOT.parent
TOOLS_DIR = APP_ROOT / 'tools'
CACHE_DIR = INSTALL_ROOT / 'cache'
JOB_CACHE = CACHE_DIR / 'jobs'
MB_CACHE = CACHE_DIR / 'musicbrainz'
MANIFEST_PATH = INSTALL_ROOT / 'install-manifest.json'
DATA_DIR = _env('BMD_DATA_DIR') or Path(os.environ.get('APPDATA') or Path.home()) / 'BraveMusicDownloader'
OUTPUT = _env('BMD_OUTPUT') or Path.home() / 'Music' / 'Brave Music Downloader'
LOG_DIR = INSTALL_ROOT / 'logs'
LOG_FILE = LOG_DIR / 'helper.log'
QUEUE_DB = DATA_DIR / 'queue.sqlite3'
LOCK_FILE = DATA_DIR / 'native-host.lock'
# Written (at most every 15 s) while the browser is talking to the helper, so setup can tell the
# user "the extension is connected" only when that is actually true. Holds no browsing data.
CONTACT_FILE = DATA_DIR / 'extension-contact.json'


def ensure_dirs():
    for folder in (DATA_DIR, OUTPUT, JOB_CACHE, MB_CACHE):
        folder.mkdir(parents=True, exist_ok=True)
