"""Brave Music Downloader 2.5.0: audio queue and processing engine (WAV / FLAC / MP3).

Runs inside the native-messaging host (stdlib only). There is no network server.
Use only with content you are authorized to save.

Reliability model
  * Downloads live in a per-job cache folder, so a crash, cancel-and-retry or
    power loss resumes (yt-dlp --continue) or replays the encode stage from the
    saved source instead of downloading again.
  * The output file is written to a hidden temp name, validated with ffprobe,
    flushed, then committed to its final name atomically and without overwriting.
  * Transient failures retry with back-off; permanent ones (private video,
    missing tool, full disk) fail immediately with a clear message.
"""
import json
import logging
import logging.handlers
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import bmdfs
import paths
from audio_core import (audio_filters, detect_bpm, encode_command, metadata_from_info,
                         format_spec, SUPPORTED_FORMATS)
from music_metadata import fetch_cover, match_track
from store import FINAL_STATES, JOB_ID, Store

VERSION = '2.5.0'
OUTPUT = paths.OUTPUT
JOB_CACHE = paths.JOB_CACHE
MB_CACHE = paths.MB_CACHE
TOOLS_DIR = paths.TOOLS_DIR
MANIFEST_PATH = paths.MANIFEST_PATH

VIDEO_ID = re.compile(r'^[a-zA-Z0-9_-]{11}$')
PLAYLIST_ID = re.compile(r'^[a-zA-Z0-9_-]{8,128}$')
PERCENT = re.compile(r'\[download\]\s+(\d+(?:\.\d+)?)%')
LOUDNESS = re.compile(r'\{\s*"input_i"\s*:\s*".*?\}', re.DOTALL)
# Matches the finished files the app writes: "Artist - Title [videoId].<ext>".
AUDIO_NAME = re.compile(r'^.+ \[([A-Za-z0-9_-]{11})\]\.(wav|flac|mp3)$', re.IGNORECASE)
MAX_ITEMS = 200
MAX_ATTEMPTS = 3
RETRY_DELAYS = (5, 15)          # seconds before attempt 2 and 3
PROGRESS_INTERVAL = 0.25        # at most 4 stored progress updates per second; state changes are never throttled
_clock = time.monotonic
MIN_FREE_BYTES = 400 * 1024 * 1024
ERROR_CACHE_DAYS = 7
STALE_TEMP_SECONDS = 3600
PERMANENT_HINTS = (
    'video unavailable', 'private video', 'this video is not available', 'members-only',
    'sign in to confirm', 'age-restricted', 'copyright', 'has been removed', 'no longer available',
    'unsupported url', 'not enough disk space', 'is not a valid url', 'not found. reinstall',
)

LOG = logging.getLogger('bmd')
LOG.addHandler(logging.NullHandler())
LOG_FILE = paths.LOG_FILE
STORE = None
ACTIVE = {}
ACTIVE_LOCK = threading.Lock()
WORKER_STATE = {'current': None}
WAKE = threading.Event()


class Canceled(Exception):
    pass


class PermanentError(RuntimeError):
    """A failure that retrying cannot fix."""


class MissingTool(PermanentError):
    pass


def configure(output=None, job_cache=None, mb_cache=None, tools_dir=None, manifest=None):
    """Redirect locations (tests, VMs). Call before the worker starts."""
    global OUTPUT, JOB_CACHE, MB_CACHE, TOOLS_DIR, MANIFEST_PATH
    OUTPUT = Path(output) if output else OUTPUT
    JOB_CACHE = Path(job_cache) if job_cache else JOB_CACHE
    MB_CACHE = Path(mb_cache) if mb_cache else MB_CACHE
    TOOLS_DIR = Path(tools_dir) if tools_dir else TOOLS_DIR
    MANIFEST_PATH = Path(manifest) if manifest else MANIFEST_PATH


def setup_logging(folder=None):
    """Rotating log (3 x 512 KB) so problems can be diagnosed from a file. Safe to call twice."""
    global LOG_FILE
    folder = Path(folder) if folder else paths.LOG_DIR
    folder.mkdir(parents=True, exist_ok=True)
    LOG_FILE = folder / 'helper.log'
    for handler in list(LOG.handlers):
        if getattr(handler, '_bmd_file', False):
            LOG.removeHandler(handler)
            handler.close()
    handler = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=512 * 1024, backupCount=3, encoding='utf-8')
    handler._bmd_file = True
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    LOG.propagate = False
    return LOG_FILE


def open_store(path=None):
    global STORE
    paths.ensure_dirs() if path is None else None
    STORE = Store(path or paths.QUEUE_DB)
    return STORE


# ---------------------------------------------------------------- URLs ----
def valid_parts(raw):
    try:
        if not isinstance(raw, str) or not 0 < len(raw) <= 2048:
            return None
        p = urlsplit(raw)
        if p.scheme != 'https' or p.port not in (None, 443) or p.username or p.password or p.fragment:
            return None
        if p.hostname not in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be'}:
            return None
        return p
    except (ValueError, TypeError):
        return None


def canonical_video(raw):
    p = valid_parts(raw)
    if not p:
        return None
    if p.hostname == 'youtu.be':
        vid = p.path.strip('/')
    elif p.path == '/watch':
        vid = parse_qs(p.query).get('v', [''])[0]
    elif re.fullmatch(r'/shorts/[A-Za-z0-9_-]{11}/?', p.path):
        vid = p.path.split('/')[2]
    else:
        return None
    return f'https://www.youtube.com/watch?v={vid}' if VIDEO_ID.fullmatch(vid) else None


def canonical_playlist(raw):
    p = valid_parts(raw)
    if not p or p.path not in ('/playlist', '/watch'):
        return None
    playlist_id = parse_qs(p.query).get('list', [''])[0]
    if not PLAYLIST_ID.fullmatch(playlist_id):
        return None
    return 'https://www.youtube.com/playlist?list=' + playlist_id


def safe_part(value, maxlen=76):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', ' ', str(value or '')).strip(' .')
    value = re.sub(r'\s+', ' ', value)[:maxlen].rstrip(' .') or 'Untitled'
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
    return '_' + value if value.split('.')[0].upper() in reserved else value


# --------------------------------------------------------------- tools ----
def exe_name(name):
    return name + '.exe' if os.name == 'nt' else name


def tool(name):
    """App-private copy first, then PATH. Never guesses."""
    local = TOOLS_DIR / exe_name(name)
    if local.is_file():
        return str(local)
    found = shutil.which(name)
    if found:
        return found
    raise MissingTool(f'{name} not found. Reinstall Brave Music Downloader (setup repairs missing tools).')


def tool_env():
    env = dict(os.environ)
    env['PATH'] = str(TOOLS_DIR) + os.pathsep + env.get('PATH', '')
    env['PYTHONUTF8'] = '1'
    return env


def _flags():
    return subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


# ------------------------------------------------------------- library ----
def find_existing(video_id):
    path = STORE.library_get(video_id)
    if path:
        p = Path(path)
        try:
            if p.is_file() and p.stat().st_size > 44:
                return p
        except OSError:
            pass
        STORE.library_forget(video_id)
    return None


LIBRARY_BATCH = 1000


def reconcile_library():
    """Index WAVs already in the save folder and forget ones that were removed.

    Writes are batched (one transaction per LIBRARY_BATCH files, unchanged rows are not rewritten) and
    only video ids are kept in memory. Also deletes this app's own stale `.processing-*` temp files (and
    only those) left behind by a crash. Sidecars without a WAV are only counted.
    """
    summary = {'indexed': 0, 'forgotten': 0, 'stale_temp_removed': 0, 'orphan_sidecars': 0}
    if not OUTPUT.is_dir():
        return summary
    seen, batch = set(), []
    now = time.time()
    for current, dirs, files in os.walk(OUTPUT, followlinks=False):
        dirs[:] = [d for d in dirs if not bmdfs.is_link(os.path.join(current, d))]
        for name in files:
            full = Path(current) / name
            if name.startswith('.processing-'):
                try:
                    if now - full.stat().st_mtime > STALE_TEMP_SECONDS:
                        bmdfs.safe_unlink(full, OUTPUT)
                        summary['stale_temp_removed'] += 1
                except OSError:
                    pass
                continue
            match = AUDIO_NAME.match(name)
            if match:
                try:
                    size = full.stat().st_size
                except OSError:
                    continue
                if size > 44:
                    seen.add(match.group(1))
                    batch.append((match.group(1), str(full), size, now))
                    if len(batch) >= LIBRARY_BATCH:
                        summary['indexed'] += STORE.library_upsert_many(batch)
                        batch.clear()
            elif name.endswith('.metadata.json') and not any(
                    (Path(current) / (name[:-len('.metadata.json')] + ext)).exists()
                    for ext in ('.wav', '.flac', '.mp3')):
                summary['orphan_sidecars'] += 1
    if batch:
        summary['indexed'] += STORE.library_upsert_many(batch)
    # An indexed id that the scan just saw points at a file that exists; only the others need a check.
    after = ''
    while True:
        rows = STORE.library_page(after)
        if not rows:
            break
        gone = [vid for vid, stored in rows if vid not in seen and not os.path.isfile(stored)]
        if gone:
            STORE.library_forget_many(gone)
            summary['forgotten'] += len(gone)
        after = rows[-1][0]
    return summary


# --------------------------------------------------------------- cache ----
def job_cache_dir(job_id):
    if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
        raise ValueError('Invalid job ID')
    return JOB_CACHE / job_id


def purge_cache(job_id):
    try:
        return bmdfs.safe_rmtree(job_cache_dir(job_id), JOB_CACHE)
    except (OSError, ValueError, bmdfs.BoundaryError):
        return 0


def sweep_cache(now=None):
    """Remove cache for finished/unknown jobs and week-old failed jobs."""
    now = now or time.time()
    removed = []
    if not JOB_CACHE.is_dir():
        return removed
    candidates = [p for p in JOB_CACHE.iterdir() if JOB_ID.fullmatch(p.name)]
    states = STORE.states([p.name for p in candidates])
    for folder in candidates:
        state = states.get(folder.name)
        drop = (state is None
                or state[0] in ('done', 'canceled')
                or (state[0] == 'error' and now - state[1] > ERROR_CACHE_DAYS * 86400))
        if drop and WORKER_STATE['current'] != folder.name:
            if purge_cache(folder.name):
                removed.append(folder.name)
    return removed


def startup_maintenance():
    result = {}
    try:
        result['library'] = reconcile_library()
        result['cache_removed'] = len(sweep_cache())
        LOG.info('Startup maintenance: %s', result)
    except Exception:  # maintenance must never stop the host
        LOG.exception('Startup maintenance failed')
    return result


# ------------------------------------------------------------ processes ----
def check_canceled(job_id):
    job = STORE.get(job_id)
    if job is None or job['state'] == 'canceled':
        raise Canceled()


def kill_process(proc):
    if proc.poll() is None:
        try:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
            else:
                proc.kill()
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass


def start_process(job_id, args, binary=False, **kwargs):
    check_canceled(job_id)
    if binary:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 text=False, creationflags=_flags(), env=tool_env(), **kwargs)
    else:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding='utf-8', errors='replace', bufsize=1, creationflags=_flags(),
                                env=tool_env(), **kwargs)
    with ACTIVE_LOCK:
        ACTIVE[job_id] = proc
    if STORE.get(job_id)['state'] == 'canceled':
        kill_process(proc)
    return proc


def end_process(job_id, proc):
    with ACTIVE_LOCK:
        if ACTIVE.get(job_id) is proc:
            ACTIVE.pop(job_id, None)
    if proc.stdout:
        proc.stdout.close()


def command(job_id, args, timeout=900, binary=False):
    proc = start_process(job_id, args, binary=binary)
    started = time.monotonic()
    try:
        while True:
            check_canceled(job_id)
            if time.monotonic() - started > timeout:
                raise TimeoutError('Processing exceeded time limit')
            try:
                output, _ = proc.communicate(timeout=.35)
                if binary:
                    return proc.returncode, output[-400000:]   # ~50s of 8 kHz f32 mono is enough
                return proc.returncode, output[-12000:]
            except subprocess.TimeoutExpired:
                continue
    finally:
        kill_process(proc)
        end_process(job_id, proc)


# ------------------------------------------------------------- download ----
def streamed_download(job_id, url, workdir, artwork=True):
    args = [tool('yt-dlp'), '--ignore-config', '--no-playlist', '--no-overwrites', '--continue',
            '--newline', '--progress', '--retries', '3', '--fragment-retries', '3',
            '--socket-timeout', '30', '--ffmpeg-location', str(TOOLS_DIR), '-f', 'bestaudio/best',
            '--write-info-json', '-o', str(workdir / 'source.%(ext)s')]
    if artwork:
        args.append('--write-thumbnail')
    args += ['--', url]
    proc = start_process(job_id, args)
    lines = queue.Queue(maxsize=128)
    logs = deque(maxlen=18)

    def reader():
        try:
            for line in proc.stdout:
                try:
                    lines.put_nowait(line)
                except queue.Full:
                    pass
        finally:
            lines.put(None)

    threading.Thread(target=reader, daemon=True).start()
    started = time.monotonic()
    ended = False
    last_sent, pending = None, None
    try:
        while not ended:
            check_canceled(job_id)
            if time.monotonic() - started > 1200:
                raise TimeoutError('Download timed out after 20 minutes')
            try:
                line = lines.get(timeout=.25)
            except queue.Empty:
                continue
            if line is None:
                ended = True
                continue
            line = line.strip()
            if line:
                logs.append(line)
            match = PERCENT.search(line)
            if match:
                pct = min(100, float(match.group(1)))
                pending = (round(5 + .68 * pct), f'Downloading… {pct:.1f}%')
                now = _clock()
                if last_sent is None or now - last_sent >= PROGRESS_INTERVAL:
                    STORE.update(job_id, stage='downloading', percent=pending[0], message=pending[1])
                    last_sent, pending = now, None
        proc.wait(timeout=10)
        check_canceled(job_id)
        if pending:                      # the last tick was held back by the throttle; never lose it
            STORE.update(job_id, stage='downloading', percent=pending[0], message=pending[1])
        if proc.returncode:
            raise RuntimeError('yt-dlp failed: ' + ' | '.join(logs)[-700:])
    finally:
        kill_process(proc)
        end_process(job_id, proc)


SOURCE_SKIP = ('.json', '.jpg', '.jpeg', '.png', '.webp', '.avif', '.part', '.ytdl')


def _source_files(folder):
    return [p for p in folder.iterdir() if p.is_file() and p.name.startswith('source.')
            and p.suffix.lower() not in SOURCE_SKIP]


def finalize_download(workdir):
    info_path = workdir / 'source.info.json'
    if not info_path.is_file():
        raise RuntimeError('yt-dlp did not produce track metadata')
    inputs = _source_files(workdir)
    if len(inputs) != 1:
        raise RuntimeError('Expected one downloaded audio source')
    source = inputs[0]
    bmdfs.write_json_atomic(workdir / 'download.json',
                            {'source': source.name, 'size': source.stat().st_size, 'info': info_path.name})
    return source, json.loads(info_path.read_text(encoding='utf-8'))


def cached_download(workdir):
    """Return (source, info) when a previous attempt finished downloading."""
    marker = bmdfs.read_json(workdir / 'download.json')
    if not isinstance(marker, dict):
        return None, None
    source, info_path = workdir / str(marker.get('source', '')), workdir / str(marker.get('info', ''))
    try:
        if source.is_file() and info_path.is_file() and source.stat().st_size == marker.get('size'):
            return source, json.loads(info_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        pass
    return None, None


# ----------------------------------------------------------- processing ----
def trim_leading_silence(job_id, source, output):
    args = [tool('ffmpeg'), '-hide_banner', '-nostdin', '-y', '-i', str(source),
            '-map', '0:a:0', '-vn', '-map_metadata', '-1',
            '-af', audio_filters(False, None, True), '-c:a', 'pcm_f32le', '-f', 'wav', str(output)]
    rc, text = command(job_id, args, 1200)
    if rc or not output.exists() or output.stat().st_size < 44:
        raise RuntimeError('FFmpeg leading silence trim failed: ' + text[-650:])
    return output


def measure_loudness(job_id, source, trim_start=False):
    args = [tool('ffmpeg'), '-hide_banner', '-nostdin', '-i', str(source), '-map', '0:a:0', '-vn',
            '-af', audio_filters(True, None, trim_start) + ':print_format=json', '-f', 'null', '-']
    rc, output = command(job_id, args, 900)
    if rc:
        raise RuntimeError('FFmpeg loudness analysis failed: ' + output[-650:])
    matches = LOUDNESS.findall(output)
    if not matches:
        raise RuntimeError('FFmpeg did not provide loudness measurements')
    measurements = json.loads(matches[-1])
    pairs = {'measured_I': 'input_i', 'measured_TP': 'input_tp', 'measured_LRA': 'input_lra',
             'measured_thresh': 'input_thresh', 'offset': 'target_offset'}
    try:
        result = {dst: float(measurements[src]) for dst, src in pairs.items()}
        return result if all(math.isfinite(x) for x in result.values()) else None
    except (KeyError, ValueError, TypeError):
        return None


def source_sample_rate(job_id, source):
    rc, output = command(job_id, [tool('ffprobe'), '-v', 'error', '-select_streams', 'a:0',
                                  '-show_entries', 'stream=sample_rate', '-of', 'default=nw=1:nk=1',
                                  str(source)], 30)
    try:
        rate = int(output.splitlines()[0])
        return rate if 8000 <= rate <= 192000 else 48000
    except (ValueError, IndexError):
        return 48000


def validate_wav(job_id, path, expected_seconds=None):
    """Prove the temp output is a complete, valid file of the requested format before commit.

    For WAV the check is strict (24-bit PCM, as before). For FLAC/MP3 ffprobe reports the
    actual codec name, so we assert it matches what we asked for and that the file is non-empty.
    """
    rc, output = command(job_id, [tool('ffprobe'), '-v', 'error', '-select_streams', 'a:0',
                                  '-show_entries', 'stream=codec_name,sample_rate:format=duration',
                                  '-of', 'json', str(path)], 60)
    try:
        data = json.loads(output)
        codec = data['streams'][0]['codec_name']
        seconds = float(data['format']['duration'])
    except (ValueError, KeyError, IndexError, TypeError):
        raise RuntimeError('FFprobe could not read the new file: ' + output[-300:]) from None
    expected_codec = {'wav': 'pcm_s24le', 'flac': 'flac', 'mp3': 'mp3'}
    if rc or codec != expected_codec.get(path.suffix.lower().lstrip('.'), codec):
        raise RuntimeError(f'Unexpected encoding: {codec}')
    if seconds < 0.5:
        raise RuntimeError('New file is empty')
    if expected_seconds and seconds < expected_seconds * 0.9 - 2:
        raise RuntimeError(f'New file looks truncated ({seconds:.0f}s of {expected_seconds:.0f}s)')
    return seconds


def fallback_thumbnail(job_id, folder, destination):
    thumbs = [p for p in folder.iterdir() if p.is_file() and p.name.startswith('source.')
              and p.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp', '.avif')]
    if not thumbs:
        return False
    rc, _ = command(job_id, [tool('ffmpeg'), '-v', 'error', '-nostdin', '-y', '-i', str(thumbs[0]),
                             '-frames:v', '1', '-q:v', '3', str(destination)], 90)
    return rc == 0 and destination.exists() and destination.stat().st_size > 100


def choose_output(tags, video_id, organize, fmt='wav'):
    spec = format_spec(fmt)
    artist = safe_part(tags.get('artist') or 'Unknown artist', 55)
    album = safe_part(tags.get('album') or 'Singles', 65)
    name = safe_part(tags.get('title') or 'Untitled', 90) + f' [{video_id}].{spec["ext"]}'
    directory = OUTPUT / artist / album if organize else OUTPUT
    return directory, name


def _encode_message(fmt, normalize):
    label = {'wav': '24-bit WAV', 'flac': 'FLAC (lossless)', 'mp3': 'MP3 (320 kbps)'}[fmt]
    suffix = ' (normalized, pass 2/2)' if normalize else ''
    return f'Writing {label}{suffix}…'


def _done_message(fmt, metadata, match, bpm):
    label = {'wav': 'WAV', 'flac': 'FLAC', 'mp3': 'MP3'}[fmt]
    parts = [f'{label} saved successfully.']
    if metadata:
        parts.append(' Metadata: ' + match['status'] + '.')
    if bpm:
        parts.append(f' BPM: {bpm}.')
    return ''.join(parts)


def check_free_space(folder):
    probe = folder if folder.exists() else folder.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if shutil.disk_usage(probe).free < MIN_FREE_BYTES:
        raise PermanentError('Not enough disk space (400 MB free needed).')


def _mark_done_existing(ident, existing):
    STORE.update(ident, state='done', stage='done', percent=100, filename=existing.name,
                 folder=str(existing.parent), message='Already downloaded; existing file preserved.')


def run_download(job):
    ident, url, video_id = job['id'], job['url'], job['video_id']
    STORE.update(ident, stage='preparing', percent=1, message='Checking tools…')
    for name in ('yt-dlp', 'ffmpeg', 'ffprobe'):
        tool(name)
    existing = find_existing(video_id)
    if existing:
        _mark_done_existing(ident, existing)
        return
    check_free_space(OUTPUT)
    work = job_cache_dir(ident)
    work.mkdir(parents=True, exist_ok=True)
    source, info = cached_download(work)
    if source is None:
        streamed_download(ident, url, work, artwork=job.get('artwork', True))
        check_canceled(ident)
        source, info = finalize_download(work)
    else:
        STORE.update(ident, stage='preparing', percent=73, message='Using the saved download…')
    check_canceled(ident)
    trim = bool(job.get('trim_start', False))
    if trim:
        STORE.update(ident, stage='trimming', percent=72, message='Trimming leading silence (lossless pre-pass)…')
        source = trim_leading_silence(ident, source, work / 'trimmed.wav')
        check_canceled(ident)
    tags = metadata_from_info(info, url)
    tags['video_id'] = video_id
    tags['normalization']['enabled'] = job['normalize']
    tags['trim_leading_silence'] = trim
    match = {'status': 'disabled', 'confidence': 0, 'recording_id': '', 'release_id': '',
             'reason': 'Metadata matching off'}
    if job['metadata'] and job.get('musicbrainz', True):
        STORE.update(ident, stage='matching', percent=73, message='Checking MusicBrainz for accurate tags…')
        tags, match = match_track(tags, MB_CACHE)
    STORE.update(ident, metadata_match=match['status'], artist=tags['artist'], album=tags.get('album', ''))
    check_canceled(ident)
    fmt = (job.get('format') or 'wav').lower()
    fmt = fmt if fmt in ('wav', 'flac', 'mp3') else 'wav'
    outdir, filename = choose_output(tags, video_id, job.get('organize', True), fmt)
    destination = outdir / filename
    measured = None
    if job['normalize']:
        STORE.update(ident, stage='analyzing', percent=77, message='Analyzing loudness (pass 1/2)…')
        measured = measure_loudness(ident, source)
    rate = source_sample_rate(ident, source)
    check_canceled(ident)
    # Detect BPM and embed it in the tags (TBPM for FLAC/MP3, kept in the sidecar for WAV).
    bpm = None
    if job['metadata']:
        STORE.update(ident, stage='beatdetect', percent=82, message='Detecting tempo (BPM)…')
        try:
            bpm = detect_bpm(command, ident, source, ffmpeg=tool('ffmpeg'))
        except Canceled:
            raise
        except Exception:  # BPM is best-effort; never let it stop a download
            bpm = None
        if bpm:
            tags['bpm'] = bpm
    STORE.update(ident, stage='encoding', percent=88,
                 message=_encode_message(fmt, job['normalize']))
    outdir.mkdir(parents=True, exist_ok=True)
    spec = format_spec(fmt)
    tempout = outdir / ('.processing-' + ident + '.' + spec['ext'])
    tempjson = outdir / ('.processing-' + ident + '.metadata.json')
    tempcover = outdir / ('.processing-' + ident + '.cover.jpg')
    try:
        # Fetch the cover BEFORE encoding so it can be embedded into FLAC/MP3; for WAV it
        # stays a sidecar. The temp file is reused for both the embedded and sidecar paths.
        artwork_source = 'none'
        have_cover = False
        if job.get('artwork', True):
            STORE.update(ident, stage='artwork', percent=92, message='Preparing artwork…')
            release_id = match.get('release_id')
            if release_id and fetch_cover(release_id, tempcover):
                artwork_source = 'MusicBrainz Cover Art Archive'
                have_cover = True
            elif fallback_thumbnail(ident, work, tempcover):
                artwork_source = 'YouTube thumbnail (fallback)'
                have_cover = True
        check_canceled(ident)
        cover_for_encode = tempcover if (have_cover and spec['embeds_art']) else None
        args = encode_command(source, tempout, tags if job['metadata'] else {}, fmt,
                              job['normalize'], rate, measured, trim, ffmpeg=tool('ffmpeg'),
                              cover=cover_for_encode)
        rc, text = command(ident, args, 1200)
        if rc:
            raise RuntimeError(f'FFmpeg {fmt.upper()} conversion failed: ' + text[-700:])
        duration = None if trim else info.get('duration')
        validate_wav(ident, tempout, duration if isinstance(duration, (int, float)) else None)
        bmdfs.fsync_file(tempout)
        check_canceled(ident)
        # Where the cover sidecar goes (WAV always; FLAC/MP3 only as an extra if requested).
        chosen_cover = None
        if have_cover:
            if spec['embeds_art']:
                # The artwork is inside the file; keep a sidecar too only when organizing by album
                # (so a folder has a visible cover.jpg) — otherwise nothing extra is written.
                chosen_cover = (outdir / 'cover.jpg') if job.get('organize', True) and tags.get('album') else None
            else:
                chosen_cover = destination.with_suffix('.cover.jpg')
        check_canceled(ident)
        with STORE.lock:
            check_canceled(ident)
            existing = find_existing(video_id)
            if existing:
                _mark_done_existing(ident, existing)
                return
            try:
                bmdfs.commit_new_file(tempout, destination)      # the one atomic step
            except FileExistsError:
                _mark_done_existing(ident, destination)
                STORE.library_put(video_id, destination, destination.stat().st_size)
                return
            size = destination.stat().st_size
            STORE.library_put(video_id, destination, size)
            notes = []
            tags.update(musicbrainz_match=match, artwork_source=artwork_source,
                        output_sample_rate=rate, output_format=fmt, bpm=bpm,
                        cover_embedded=spec['embeds_art'] and have_cover)
            if job['metadata']:
                try:
                    tempjson.write_text(json.dumps(tags, indent=2, ensure_ascii=False), encoding='utf-8')
                    bmdfs.commit_new_file(tempjson, destination.with_suffix('.metadata.json'))
                except OSError:
                    notes.append('metadata file not saved')
            if chosen_cover and tempcover.is_file():
                try:
                    bmdfs.commit_new_file(tempcover, chosen_cover)
                except FileExistsError:
                    pass  # the album already has a front cover; keep it
                except OSError:
                    notes.append('cover not saved')
            STORE.update(ident, state='done', stage='done', percent=100,
                         message=_done_message(fmt, job['metadata'], match, bpm),
                         filename=destination.name, folder=str(destination.parent), artist=tags['artist'],
                         album=tags.get('album') or 'Singles', metadata_match=match['status'],
                         artwork_source=artwork_source, size_bytes=size)
    finally:
        for temp in (tempout, tempjson, tempcover):
            temp.unlink(missing_ok=True)


# --------------------------------------------------------------- worker ----
def is_permanent(exc):
    if isinstance(exc, (PermanentError, ValueError)):
        return True
    text = str(exc).lower()
    return any(hint in text for hint in PERMANENT_HINTS)


def _sleep_unless_canceled(ident, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if STORE.get(ident)['state'] == 'canceled':
            return
        time.sleep(min(.25, max(0, deadline - time.monotonic())))


def process_job(ident):
    """Run one job to a final state, retrying transient failures."""
    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if STORE.get(ident)['state'] == 'canceled':
                break
            STORE.update(ident, state='running', attempts=attempt,
                         message=f'Attempt {attempt}/{MAX_ATTEMPTS}…')
            LOG.info('Job %s (video %s): attempt %d/%d', ident, STORE.get(ident)['video_id'], attempt, MAX_ATTEMPTS)
            try:
                run_download(STORE.get(ident))
                LOG.info('Job %s: %s', ident, STORE.get(ident)['message'])
                break
            except Canceled:
                break
            except Exception as exc:
                if STORE.get(ident)['state'] == 'canceled':
                    break
                message = str(exc).strip()[:750] or 'Unexpected processing error'
                LOG.warning('Job %s attempt %d failed (%s): %s', ident, attempt,
                            'permanent' if is_permanent(exc) else 'transient', message)
                if attempt == MAX_ATTEMPTS or is_permanent(exc):
                    STORE.update(ident, state='error', stage='error', percent=None, message=message,
                                 last_error=message)
                    break
                STORE.update(ident, stage='retrying', percent=0, last_error=message,
                             message=f'Retrying in {RETRY_DELAYS[attempt - 1]}s after error: {message}')
                _sleep_unless_canceled(ident, RETRY_DELAYS[attempt - 1])
    finally:
        state = (STORE.get(ident) or {}).get('state')
        if state in ('done', 'canceled'):
            purge_cache(ident)           # failed jobs keep their cache so Retry resumes


def worker_loop():
    while True:
        with ACTIVE_LOCK:
            job = STORE.next_queued()
            if job:
                WORKER_STATE['current'] = job['id']
        if not job:
            WAKE.wait(1)
            WAKE.clear()
            continue
        try:
            process_job(job['id'])
        except Exception:
            LOG.exception('Worker error on job %s', job['id'])
        finally:
            with ACTIVE_LOCK:
                WORKER_STATE['current'] = None


# ------------------------------------------------------------- playlist ----
def preview_playlist(url):
    canonical = canonical_playlist(url)
    if not canonical:
        raise ValueError('Provide a valid YouTube playlist URL')
    try:
        ytdlp = tool('yt-dlp')
    except MissingTool as exc:
        raise ValueError(str(exc)) from None
    args = [ytdlp, '--ignore-config', '--flat-playlist', '--dump-single-json', '--playlist-end',
            str(MAX_ITEMS), '--no-warnings', '--', canonical]
    try:
        result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', errors='replace',
                                timeout=120, creationflags=_flags(), env=tool_env())
    except subprocess.TimeoutExpired:
        raise ValueError('Playlist preview timed out; try a smaller playlist') from None
    if result.returncode:
        raise ValueError('Playlist import failed: ' + result.stderr[-550:])
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise ValueError('Playlist response was not valid JSON') from None
    items, seen = [], set()
    for entry in info.get('entries') or []:
        if not isinstance(entry, dict):
            continue
        vid = entry.get('id')
        if not isinstance(vid, str) or not VIDEO_ID.fullmatch(vid) or vid in seen:
            continue
        seen.add(vid)
        items.append({'id': vid, 'title': str(entry.get('title') or 'Unknown track')[:240],
                      'artist': str(entry.get('uploader') or entry.get('channel') or '')[:140],
                      'duration': entry.get('duration') if isinstance(entry.get('duration'), (int, float)) else None})
    return {'title': str(info.get('title') or 'YouTube playlist')[:220], 'items': items,
            'count': len(items), 'limit': MAX_ITEMS, 'truncated': len(items) >= MAX_ITEMS}


# ---------------------------------------------------------- diagnostics ----
def _tool_version(path):
    try:
        run = subprocess.run([path, '--version'], capture_output=True, text=True, timeout=8,
                             errors='replace', creationflags=_flags(), env=tool_env())
        line = (run.stdout or run.stderr).splitlines()
        return run.returncode == 0, (line[0] if line else '')[:140]
    except (OSError, subprocess.TimeoutExpired):
        return False, 'Execution failed'


def file_sha256(path, chunk=1 << 20):
    """SHA-256 of a file, streamed so a 100 MB tool never has to fit in memory."""
    import hashlib
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(chunk), b''):
            digest.update(block)
    return digest.hexdigest()


def manifest_summary():
    manifest = bmdfs.read_json(MANIFEST_PATH)
    if not isinstance(manifest, dict):
        return {'present': False, 'components': {}}
    comps = {c.get('name'): c for c in manifest.get('components', []) if isinstance(c, dict)}
    return {'present': True, 'version': manifest.get('version'), 'components': comps}


def diagnostics(deep=False):
    summary = manifest_summary()
    tools, issues = {}, []
    for name in ('yt-dlp', 'ffmpeg', 'ffprobe', 'deno'):
        try:
            path = tool(name)
        except MissingTool:
            tools[name] = {'installed': False, 'version': '', 'path': '', 'source': 'missing', 'owned': False}
            issues.append(f'{name} is missing')
            continue
        ok, version = _tool_version(path)
        entry = summary['components'].get(name, {})
        info = {'installed': ok, 'version': version, 'path': path,
                'source': 'app' if bmdfs.is_within(path, TOOLS_DIR) else 'system',
                'owned': bool(entry.get('owned')), 'verified_by': entry.get('verified_by', '')}
        if deep and info['source'] == 'app':
            # Compare with the hash of the installed FILE (recorded at install time), never with the archive's hash.
            recorded = entry.get('file_sha256')
            info['hash_matches_install'] = (file_sha256(path) == recorded) if recorded else None   # None = unknown
        if not ok:
            issues.append(f'{name} could not run')
        tools[name] = info
    free = None
    try:
        probe = OUTPUT if OUTPUT.exists() else OUTPUT.parent
        free = shutil.disk_usage(probe).free
    except OSError:
        pass
    if free is not None and free < MIN_FREE_BYTES:
        issues.append('Less than 400 MB free in the save folder')
    cache_bytes = bmdfs.dir_size(JOB_CACHE) if JOB_CACHE.is_dir() else 0
    return {'app_version': VERSION, 'python': sys.version.split()[0], 'tools': tools,
            'requirements_ok': all(t['installed'] for t in tools.values()),
            'issues': issues, 'output': str(OUTPUT), 'log_file': str(LOG_FILE), 'free_bytes': free, 'cache_bytes': cache_bytes,
            'database': STORE.info() if STORE else None,
            'install': {'manifest_present': summary['present'], 'installed_version': summary.get('version')},
            'hint': 'YouTube needs a current yt-dlp with its JavaScript challenge solver and the Deno runtime.'}
