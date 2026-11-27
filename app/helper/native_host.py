"""Brave Music Downloader 2.5.0: native-messaging stdio host.

Launched by the Windows native launcher. Stdout carries ONLY little-endian
length-prefixed JSON messages; everything else goes to stderr.
"""
import json
import os
import re
import struct
import sys
import threading
import time
import uuid
from contextlib import contextmanager

import bmdfs
import helper as core
import paths

VERSION = core.VERSION
MAX_MESSAGE = 1024 * 1024
HOST = 'com.bravemusic.downloader'
BOOT_ID = uuid.uuid4().hex
LOCK_WAIT_SECONDS = 10
LOCK_POLL_SECONDS = 0.05      # how often a starting helper retries the lock while the old one exits
METHODS = {'health', 'diagnostics', 'jobs', 'status', 'preview', 'enqueue', 'cancel', 'retry', 'clear'}
JOB_ID = re.compile(r'^[0-9a-f]{32}$')


class ProtocolError(Exception):
    """Framing is broken; the stream cannot be trusted any more."""


class BadMessage(Exception):
    """A complete frame that was not valid JSON; the stream is still in sync."""


def read_exact(stream, count):
    chunks = []
    while count:
        chunk = stream.read(count)
        if not chunk:
            return None
        chunks.append(chunk)
        count -= len(chunk)
    return b''.join(chunks)


def read_packet(stream):
    header = read_exact(stream, 4)
    if header is None:
        return None
    size = struct.unpack('<I', header)[0]
    if size == 0 or size > MAX_MESSAGE:
        raise ProtocolError('Native message exceeds 1 MiB or is empty')
    data = read_exact(stream, size)
    if data is None:
        raise ProtocolError('Truncated native message')
    try:
        return json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        raise BadMessage('Malformed JSON') from None


def write_packet(stream, value):
    encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(encoded) > MAX_MESSAGE:
        raise ValueError('Native response exceeds 1 MiB')
    stream.write(struct.pack('<I', len(encoded)))
    stream.write(encoded)
    stream.flush()


DIAG_CACHE = {'at': 0.0, 'value': None}


def diagnostics(deep=False):
    if deep or DIAG_CACHE['value'] is None or time.monotonic() - DIAG_CACHE['at'] > 30:
        DIAG_CACHE.update(value=core.diagnostics(deep=deep), at=time.monotonic())
    return DIAG_CACHE['value']


def _job_id(params):
    job_id = params.get('id')
    if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
        raise ValueError('Invalid job ID')
    return job_id


def _jobs(params):
    limit, cursor, state = params.get('limit', 50), params.get('cursor'), params.get('state')
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError('limit must be 1-200')
    if cursor is not None and not isinstance(cursor, str):
        raise ValueError('Invalid page cursor')
    if state is not None and not isinstance(state, str):
        raise ValueError('Invalid state filter')
    jobs, next_cursor = core.STORE.page(limit, cursor, state)
    return {'jobs': jobs, 'counts': core.STORE.counts(), 'next_cursor': next_cursor}


def dispatch(method, params):
    if not isinstance(params, dict):
        raise ValueError('Expected JSON object parameters')
    if method == 'health':
        return {'ok': True, 'version': VERSION, 'transport': 'nativeMessaging', 'boot_id': BOOT_ID,
                'format': 'WAV/FLAC/MP3', 'output': str(core.OUTPUT),
                'formats': list(core.SUPPORTED_FORMATS),
                'features': ['queue', 'playlist', 'musicbrainz', 'artwork', 'organize', 'trim_start',
                             'page_actions', 'diagnostics', 'resume', 'atomic_wav', 'pagination',
                             'multi_format', 'embedded_artwork', 'bpm']}
    if method == 'diagnostics':
        return diagnostics(deep=params.get('deep') is True)
    if method == 'jobs':
        return _jobs(params)
    if method == 'status':
        result = core.STORE.get(_job_id(params))
        if result is None:
            raise ValueError('Unknown job')
        return result
    if method == 'preview':
        return core.preview_playlist(params.get('url'))
    if method == 'enqueue':
        fmt = (params.get('format') or 'wav').lower()
        if fmt not in core.SUPPORTED_FORMATS:
            raise ValueError('Unsupported format; choose wav, flac or mp3')
        meta, norm = params.get('metadata'), params.get('normalize')
        if not isinstance(meta, bool) or not isinstance(norm, bool):
            raise ValueError('Invalid metadata/normalization options')
        other = {name: params.get(name, default) for name, default in
                 [('musicbrainz', True), ('artwork', True), ('organize', True), ('trim_start', False)]}
        if not all(isinstance(value, bool) for value in other.values()):
            raise ValueError('Invalid optional settings')
        if 'url' in params:
            url = core.canonical_video(params.get('url'))
            if not url:
                raise ValueError('Provide a valid YouTube track URL')
            title = params.get('title') or 'YouTube track'
            if not isinstance(title, str):
                raise ValueError('Invalid track title')
            items = [{'id': url.split('v=')[1], 'title': title[:240]}]
        else:
            items = params.get('items')
            if not isinstance(items, list) or not 1 <= len(items) <= core.MAX_ITEMS:
                raise ValueError('Select 1-200 tracks')
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get('id'), str) \
                        or not core.VIDEO_ID.fullmatch(item['id']):
                    raise ValueError('Invalid playlist track ID')
                if not isinstance(item.get('title', ''), str) or len(item.get('title', '')) > 240:
                    raise ValueError('Invalid playlist title')
        outcome = core.STORE.enqueue(items, meta, norm, find_existing=core.find_existing, format=fmt, **other)
        if outcome['added']:
            core.WAKE.set()
        return outcome
    if method in ('cancel', 'retry'):
        job_id = _job_id(params)
        if method == 'cancel':
            job = core.STORE.cancel(job_id)
            with core.ACTIVE_LOCK:
                proc = core.ACTIVE.get(job_id)
            if proc:
                core.kill_process(proc)
        else:
            with core.ACTIVE_LOCK:
                if core.WORKER_STATE['current'] == job_id:
                    raise ValueError('Download still stopping; retry afterward')
                job = core.STORE.retry(job_id, find_existing=core.find_existing)
            core.WAKE.set()
        return {'job': job}
    if method == 'clear':
        removed = core.STORE.clear_finished()
        core.sweep_cache()
        return {'removed': removed}
    raise ValueError('Unsupported native request')


def _try_lock(file):
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(file):
    file.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)


@contextmanager
def singleton_lock(path, wait=LOCK_WAIT_SECONDS):
    """One host per queue database.

    When the extension reconnects right after a disconnect, the previous host
    may still be shutting down, so wait briefly before giving up.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a+b') as file:
        file.seek(0, 2)
        if file.tell() == 0:
            file.write(b'0')
            file.flush()
        file.seek(0)
        deadline = time.monotonic() + wait
        while True:
            try:
                _try_lock(file)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Another Brave Music Downloader helper is already running') from None
                time.sleep(LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            _unlock(file)


CONTACT_INTERVAL = 15         # seconds between writes of the "the browser is talking to me" marker
_CONTACT_AT = [0.0]


def mark_contact(method):
    """Record that the browser (not setup's own health check) just talked to the helper.

    Setup waits for this to tell the user the extension is really connected. Nothing is written
    for setup's own round trips (they set BMD_NO_CONTACT_MARK), and a failure here is ignored:
    the marker is a convenience for the installer, never a reason to fail a request.
    """
    if os.environ.get('BMD_NO_CONTACT_MARK'):
        return
    now = time.time()
    if now - _CONTACT_AT[0] < CONTACT_INTERVAL:
        return
    _CONTACT_AT[0] = now
    try:
        paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
        bmdfs.write_json_atomic(paths.CONTACT_FILE,
                                {'time': now, 'version': VERSION, 'method': method if method in METHODS else None})
    except OSError:
        pass


def run(input_stream, output_stream, contact=False):
    while True:
        try:
            packet = read_packet(input_stream)
        except BadMessage as error:
            write_packet(output_stream, {'id': None, 'ok': False, 'error': str(error)})
            continue
        except ProtocolError as error:
            core.LOG.warning('Native protocol error: %s', error)
            return
        if packet is None:
            return
        identifier = packet.get('id') if isinstance(packet, dict) else None
        try:
            if not isinstance(packet, dict) or isinstance(identifier, bool) or not isinstance(identifier, int) \
                    or not 0 <= identifier < 2 ** 31:
                raise ValueError('Malformed message ID')
            method = packet.get('method')
            if method not in METHODS:
                raise ValueError('Unknown command')
            answer = {'id': identifier, 'ok': True, 'data': dispatch(method, packet.get('params', {}))}
        except (ValueError, KeyError, TypeError) as error:
            answer = {'id': identifier if isinstance(identifier, int) else None, 'ok': False,
                      'error': str(error)[:400]}
        except Exception as error:
            core.LOG.exception('Native request failed: %s', packet.get('method') if isinstance(packet, dict) else '?')
            answer = {'id': identifier if isinstance(identifier, int) else None, 'ok': False,
                      'error': 'Native host internal error'}
        if contact and isinstance(packet, dict) and isinstance(identifier, int):
            mark_contact(packet.get('method'))
        write_packet(output_stream, answer)


def installer_busy():
    """True while Setup/Uninstall is running (its lock names a live process).

    The browser restarts a dropped helper within seconds; a helper that started mid-install would hold
    files open and make the folder swap fail, so it declines until setup is done.
    """
    if os.environ.get('BMD_ALLOW_DURING_INSTALL'):
        return False
    lock = paths.INSTALL_ROOT / '.install.lock'
    try:
        pid = int(lock.read_text().strip() or 0)
        return time.time() - lock.stat().st_mtime < 1800 and bmdfs.pid_alive(pid)
    except (OSError, ValueError):
        return False


def main():
    if installer_busy():
        print('Brave Music Downloader setup is running; the helper starts again afterwards.', file=sys.stderr)
        return 3
    try:
        paths.ensure_dirs()
        core.setup_logging()
        core.LOG.info('Helper %s starting (pid %d)', VERSION, os.getpid())
        with singleton_lock(paths.LOCK_FILE):
            core.open_store()
            threading.Thread(target=core.startup_maintenance, daemon=True, name='bmd-maintenance').start()
            threading.Thread(target=core.worker_loop, daemon=True, name='wav-native-worker').start()
            try:
                run(sys.stdin.buffer, sys.stdout.buffer, contact=True)
            finally:
                with core.ACTIVE_LOCK:
                    processes = list(core.ACTIVE.values())
                for proc in processes:
                    core.kill_process(proc)
                core.WAKE.set()
    except Exception as exc:
        core.LOG.exception('Native host fatal')
        print('Native host fatal: ' + repr(exc), file=sys.stderr)
        return 1
    core.LOG.info('Helper stopped')
    return 0


if __name__ == '__main__':
    sys.exit(main())
