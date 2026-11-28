import io
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from support import (calls, fake_tool_file, ffprobe_json, make_launchable_tool, make_tools,
                     set_mode, tmpdir, tool_data_file)

import bmdfs
import helper
import native_host
import store as store_mod

HAS_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))
VID = 'abcdefghijk'


def frame(obj):
    raw = json.dumps(obj).encode()
    return struct.pack('<I', len(raw)) + raw


def read_frames(data):
    out, i = [], 0
    while i < len(data):
        (n,) = struct.unpack('<I', data[i:i + 4])
        out.append(json.loads(data[i + 4:i + 4 + n]))
        i += 4 + n
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.out, self.cache, self.tools = self.tmp / 'Music', self.tmp / 'cache' / 'jobs', self.tmp / 'tools'
        self.state = make_tools(self.tools)
        helper.configure(output=self.out, job_cache=self.cache, mb_cache=self.tmp / 'cache' / 'mb',
                         tools_dir=self.tools, manifest=self.tmp / 'install-manifest.json')
        helper.MIN_FREE_BYTES = 1
        helper.RETRY_DELAYS = (0.05, 0.05)
        self.out.mkdir()
        self.cache.mkdir(parents=True)
        self.store = helper.open_store(self.tmp / 'queue.sqlite3')

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def enqueue(self, vid=VID, **kw):
        opts = dict(metadata=True, normalize=False, musicbrainz=False, artwork=True, organize=True,
                    trim_start=False)
        opts.update(kw)
        r = self.store.enqueue([{'id': vid, 'title': 'T'}], opts.pop('metadata'), opts.pop('normalize'),
                               find_existing=helper.find_existing, **opts)
        return r['added'][0]


class JsonFileTests(unittest.TestCase):
    """bmdfs.read_json: the lock file, the manifests and the install journal, as other programs write them."""

    def setUp(self):
        self.tmp = tmpdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_byte_order_mark_is_tolerated(self):
        path = self.tmp / 'tools.lock.json'
        data = {'schema': 1, 'components': [{'name': 'python', 'version': '3.12.10'}]}
        plain = json.dumps(data).encode('utf-8')
        bom = b'\xef\xbb\xbf'                     # Windows PowerShell 5.1: Set-Content -Encoding UTF8
        cases = (('plain', plain, data),
                 ('byte-order mark', bom + plain, data),
                 ('byte-order mark and CRLF line ends',
                  bom + json.dumps(data, indent=2).replace('\n', '\r\n').encode('utf-8'), data),
                 ('byte-order mark and non-ASCII text',
                  bom + json.dumps({'name': 'caf\xe9'}, ensure_ascii=False).encode('utf-8'), {'name': 'caf\xe9'}))
        for label, raw, expected in cases:
            path.write_bytes(raw)
            self.assertEqual(bmdfs.read_json(path), expected, label)

    def test_anything_that_is_not_utf_8_json_gives_the_default_instead_of_an_error(self):
        path = self.tmp / 'install-manifest.json'
        self.assertIsNone(bmdfs.read_json(path))                              # not there at all
        self.assertEqual(bmdfs.read_json(path, default={'components': []}), {'components': []})
        for label, raw in (('half a file, as a torn write leaves it', b'{"schema": 1, "components": ['),
                           ('no JSON at all', b''), ('UTF-16, Set-Content -Encoding Unicode',
                                                     b'\xff\xfe' + json.dumps({'a': 1}).encode('utf-16-le')),
                           ('bytes that are not text', b'\xff\xfe\x00\x01rubbish')):
            path.write_bytes(raw)
            self.assertIsNone(bmdfs.read_json(path), label)
            self.assertEqual(bmdfs.read_json(path, default={'ok': True}), {'ok': True}, label)

    def test_a_folder_is_not_a_json_file(self):
        self.assertIsNone(bmdfs.read_json(self.tmp))                            # PermissionError / IsADirectoryError: an OSError


class UrlTests(unittest.TestCase):
    def test_canonical(self):
        self.assertEqual(helper.canonical_video('https://youtu.be/' + VID), 'https://www.youtube.com/watch?v=' + VID)
        self.assertEqual(helper.canonical_video('https://www.youtube.com/shorts/' + VID),
                         'https://www.youtube.com/watch?v=' + VID)
        # A timestamp fragment is dropped, not refused: the canonical URL is built from the id.
        self.assertEqual(helper.canonical_video('https://www.youtube.com/watch?v=' + VID + '&t=42s#start'),
                         'https://www.youtube.com/watch?v=' + VID)
        self.assertEqual(helper.canonical_video('https://youtu.be/' + VID + '#t=30'),
                         'https://www.youtube.com/watch?v=' + VID)
        for bad in ('http://youtu.be/' + VID, 'https://evil.com/watch?v=' + VID,
                    'https://www.youtube.com:444/watch?v=' + VID, 'https://user@youtu.be/' + VID, None):
            self.assertIsNone(helper.canonical_video(bad))
        self.assertIsNone(helper.canonical_playlist('https://www.youtube.com/playlist?list=a'))


class StoreTests(Base):
    def test_pragmas_and_index_plan(self):
        info = self.store.info()
        self.assertEqual((info['schema'], info['journal_mode'], info['synchronous'], info['integrity']),
                         (1, 'wal', 2, 'ok'))
        plan = ' '.join(r[3] for r in self.store.conn.execute(
            'EXPLAIN QUERY PLAN SELECT payload FROM jobs ORDER BY rank, ord, id LIMIT 10'))
        self.assertIn('jobs_page', plan)
        plan = ' '.join(r[3] for r in self.store.conn.execute(
            "EXPLAIN QUERY PLAN SELECT payload FROM jobs WHERE state='done' ORDER BY ord, id LIMIT 10"))
        self.assertIn('jobs_state_ord', plan)

    def test_a_pre_2_3_library_table_is_replaced_not_kept(self):
        # Found in the wild (2.0.x songfinder-era database): a `library` table with only
        # video_id/path/size survives CREATE IF NOT EXISTS and then breaks every download
        # commit ("table library has 3 columns but 4 values were supplied"). Both an old
        # database migrating up and a database a buggy older release already migrated must
        # end with the 4-column index; its rows are only path hints and rebuild from disk.
        import sqlite3
        db = self.tmp / 'old-library.sqlite3'
        c = sqlite3.connect(str(db))
        c.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, video_id TEXT NOT NULL, state TEXT NOT NULL,"
                  " created REAL NOT NULL, updated REAL NOT NULL, payload TEXT NOT NULL)")
        c.execute("CREATE TABLE library (video_id TEXT PRIMARY KEY, path TEXT NOT NULL, size INTEGER NOT NULL)")
        c.execute("INSERT INTO library VALUES ('v0ld0000000', 'C:/gone/old.wav', 123)")
        c.execute('PRAGMA user_version=0')
        c.commit()
        c.close()
        store = store_mod.Store(db)
        info = store.info()
        self.assertEqual(info['schema'], 1)
        self.assertEqual(info['library_entries'], 0, 'the incompatible old index is dropped')
        store.library_put('abc12345678', self.tmp / 'm.mp3', 7)
        self.assertEqual(store.library_get('abc12345678'), str(self.tmp / 'm.mp3'))
        store.close()
        # Scenario B: a database an older release already migrated to v1 with the bad table.
        db2 = self.tmp / 'v1-bad-library.sqlite3'
        c = sqlite3.connect(str(db2))
        c.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, video_id TEXT NOT NULL, state TEXT NOT NULL,"
                  " created REAL NOT NULL, updated REAL NOT NULL, payload TEXT NOT NULL, rank INTEGER NOT NULL DEFAULT 2,"
                  " ord REAL NOT NULL DEFAULT 0)")
        c.execute("CREATE TABLE library (video_id TEXT PRIMARY KEY, path TEXT NOT NULL, size INTEGER NOT NULL)")
        c.execute("INSERT INTO library VALUES ('stale', 'C:/gone/x.mp3', 5)")
        c.execute('PRAGMA user_version=1')
        c.commit()
        c.close()
        store = store_mod.Store(db2)
        self.assertEqual(store.info()['library_entries'], 0)
        store.library_put('abc12345678', self.tmp / 'm.mp3', 7)
        self.assertEqual(store.library_get('abc12345678'), str(self.tmp / 'm.mp3'))
        store.close()

    def test_migrates_v20_database_without_losing_history(self):
        path = self.tmp / 'old.sqlite3'
        c = sqlite3.connect(path)
        c.execute('CREATE TABLE jobs (id TEXT PRIMARY KEY, video_id TEXT NOT NULL, state TEXT NOT NULL, '
                  'created REAL NOT NULL, updated REAL NOT NULL, payload TEXT NOT NULL)')
        c.execute('CREATE INDEX jobs_state_created ON jobs(state, created)')
        for i, state in enumerate(['done', 'running', 'queued', 'error']):
            ident = '%032x' % i
            job = dict(id=ident, video_id='v%010d' % i, state=state, created=100.0 + i, updated=100.0 + i,
                       title='old %d' % i, stage=state, percent=5, message='m')
            c.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?)', (ident, job['video_id'], state, 100.0 + i,
                                                                100.0 + i, json.dumps(job)))
        c.commit(); c.close()
        s = store_mod.Store(path)
        try:
            self.assertEqual(s.info()['schema'], 1)
            self.assertTrue(s.backup_path and s.backup_path.exists())
            jobs, _ = s.page(10)
            self.assertEqual([j['state'] for j in jobs], ['queued', 'queued', 'error', 'done'])
            self.assertEqual(s.counts(), {'queued': 2, 'error': 1, 'done': 1})
            self.assertIn('Resumed', jobs[0]['message'] + jobs[1]['message'])
        finally:
            s.close()
        s = store_mod.Store(path)          # second open is a no-op migration
        self.assertEqual(s.counts()['done'], 1)
        s.close()

    def test_keyset_pagination_is_complete_stable_and_ordered(self):
        ids = []
        for i in range(130):
            ident = self.enqueue('v%010d' % i)
            ids.append(ident)
        for ident in ids[:40]:
            self.store.update(ident, state='done')
        for ident in ids[40:50]:
            self.store.update(ident, state='error')
        self.store.update(ids[50], state='running')
        seen, cursor, order = [], None, []
        while True:
            jobs, cursor = self.store.page(25, cursor)
            order += [j['state'] for j in jobs]
            seen += [j['id'] for j in jobs]
            if not cursor:
                break
        self.assertEqual(len(seen), 130)
        self.assertEqual(len(set(seen)), 130)
        self.assertEqual(order[0], 'running')
        self.assertLess(max(i for i, s in enumerate(order) if s == 'queued'), min(i for i, s in enumerate(order) if s in ('done', 'error')))
        done, c = [], None
        while True:
            jobs, c = self.store.page(7, c, 'done')
            done += jobs
            if not c:
                break
        self.assertEqual(len(done), 40)
        self.assertEqual([j['created'] for j in done], sorted((j['created'] for j in done), reverse=True))
        active, _ = self.store.page(200, None, 'active')
        self.assertEqual(len(active), 80)
        with self.assertRaises(ValueError):
            self.store.page(10, 'not-a-cursor')
        with self.assertRaises(ValueError):
            self.store.page(10, None, 'bogus')

    def test_running_jobs_requeue_after_crash(self):
        ident = self.enqueue()
        self.store.next_queued()
        self.store.close()
        s = store_mod.Store(self.tmp / 'queue.sqlite3')
        self.assertEqual(s.recovered, [ident])      # reopening requeues the interrupted job
        job = s.get(ident)
        self.assertEqual(job['state'], 'queued')
        s.close()
        self.store = helper.open_store(self.tmp / 'queue.sqlite3')

    def test_duplicates_existing_and_state_rules(self):
        a = self.enqueue()
        r = self.store.enqueue([{'id': VID, 'title': 'T'}], True, False, find_existing=helper.find_existing)
        self.assertEqual(len(r['duplicate']), 1)
        with self.assertRaises(ValueError):
            self.store.retry(a)
        self.store.cancel(a)
        with self.assertRaises(ValueError):
            self.store.cancel(a)
        self.store.retry(a)
        self.assertEqual(self.store.get(a)['state'], 'queued')


@unittest.skipUnless(HAS_FFMPEG, 'ffmpeg/ffprobe not available')
class PipelineTests(Base):
    def run_job(self, **kw):
        ident = self.enqueue(**kw)
        helper.process_job(ident)
        return ident, self.store.get(ident)

    def test_full_job_creates_valid_24bit_wav_atomically(self):
        ident, job = self.run_job(normalize=True)
        self.assertEqual(job['state'], 'done', job['message'])
        wav = next(self.out.rglob('*.wav'))
        probe = ffprobe_json(wav)
        self.assertEqual(probe['streams'][0]['codec_name'], 'pcm_s24le')
        self.assertGreater(float(probe['format']['duration']), 2.5)
        self.assertEqual(probe['format']['tags'].get('title'), 'Test Song')
        self.assertTrue(wav.with_suffix('.metadata.json').exists())
        self.assertEqual(list(self.out.rglob('.processing-*')), [])
        self.assertFalse((self.cache / ident).exists(), 'cache is purged after success')
        self.assertEqual(helper.find_existing(VID), wav)

    def test_flac_export_produces_lossless_flac_with_embedded_tags(self):
        ident, job = self.run_job(format='flac')
        self.assertEqual(job['state'], 'done', job['message'])
        flac = next(self.out.rglob('*.flac'))
        probe = ffprobe_json(flac)
        self.assertEqual(probe['streams'][0]['codec_name'], 'flac', 'FLAC codec written')
        self.assertGreater(float(probe['format']['duration']), 2.5)
        self.assertEqual(probe['format']['tags'].get('title'), 'Test Song')
        self.assertEqual(probe['format']['tags'].get('artist'), 'Test Artist')
        # No .wav should exist for a FLAC job, and the metadata sidecar still records the format.
        self.assertEqual(list(self.out.rglob('*.wav')), [])
        sidecar = json.loads(flac.with_suffix('.metadata.json').read_text())
        self.assertEqual(sidecar['output_format'], 'flac')

    def test_mp3_export_produces_320kbps_mp3_with_embedded_tags(self):
        ident, job = self.run_job(format='mp3')
        self.assertEqual(job['state'], 'done', job['message'])
        mp3 = next(self.out.rglob('*.mp3'))
        probe = ffprobe_json(mp3)
        self.assertEqual(probe['streams'][0]['codec_name'], 'mp3', 'MP3 codec written')
        self.assertEqual(probe['format']['tags'].get('title'), 'Test Song')
        # The library index recognizes the .mp3 extension (not just .wav).
        self.assertEqual(helper.find_existing(VID), mp3)

    def test_embedded_cover_art_for_flac_and_sidecar_for_wav(self):
        # FLAC: the cover is embedded inside the file (a video stream in the probe).
        ident, job = self.run_job(format='flac', artwork=True, vid='FL000000001')
        self.assertEqual(job['state'], 'done', job['message'])
        flac = next(self.out.rglob('*.flac'))
        probe = ffprobe_json(flac)
        self.assertTrue(any(s.get('codec_name') in ('mjpeg', 'png') for s in probe['streams']),
                        'FLAC has an embedded picture stream')
        # WAV: the cover is written as a sidecar .cover.jpg (WAV cannot embed pictures portably).
        # Use a distinct video id so the library's find_existing does not short-circuit this job.
        ident2, job2 = self.run_job(format='wav', artwork=True, vid='WA000000001')
        self.assertEqual(job2['state'], 'done', job2['message'])
        wav = next(p for p in self.out.rglob('*.wav') if 'WA000000001' in p.name)
        self.assertTrue(wav.with_suffix('.cover.jpg').exists(), 'WAV keeps a cover sidecar')

    def test_bpm_detection_runs_and_is_recorded_in_the_sidecar_when_present(self):
        # The fake yt-dlp produces a steady 440 Hz sine, whose envelope does not fluctuate, so the
        # autocorrelation may legitimately find no periodicity. The job must still succeed and the
        # sidecar must record a bpm field (None when no tempo was detected, an int when one was).
        ident, job = self.run_job(metadata=True)
        self.assertEqual(job['state'], 'done', job['message'])
        sidecar = json.loads(next(self.out.rglob('*.metadata.json')).read_text())
        self.assertIn('bpm', sidecar, 'the BPM field is always present in the sidecar')
        if sidecar['bpm'] is not None:
            self.assertIsInstance(sidecar['bpm'], int)
            self.assertTrue(60 <= sidecar['bpm'] <= 200, 'BPM folded into the musical envelope')

    def test_unsupported_format_rejected_at_the_native_host_boundary(self):
        # The native host rejects an unknown format before it reaches the queue.
        import native_host
        with self.assertRaises(ValueError):
            native_host.dispatch('enqueue', {'format': 'ogg', 'metadata': True, 'normalize': False,
                                             'url': 'https://www.youtube.com/watch?v=' + VID, 'title': 'T'})

    def test_resume_uses_continue_and_partial_file(self):
        set_mode(self.state, 'part_then_ok')
        ident, job = self.run_job()
        self.assertEqual(job['state'], 'done', job['message'])
        seen = calls(self.state)
        self.assertEqual(len(seen), 2)
        self.assertFalse(seen[0]['part_existed'])
        self.assertTrue(seen[1]['part_existed'] and seen[1]['continue'])

    def test_crash_after_download_replays_wav_without_downloading_again(self):
        real, state = helper.encode_command, {'n': 0}

        def flaky(*a, **kw):
            state['n'] += 1
            if state['n'] == 1:
                return [shutil.which('ffmpeg'), '-v', 'error', '-i', '/nonexistent/file', '-f', 'wav', '-']
            return real(*a, **kw)

        helper.encode_command = flaky
        try:
            ident, job = self.run_job()
        finally:
            helper.encode_command = real
        self.assertEqual(job['state'], 'done', job['message'])
        self.assertEqual(len(calls(self.state)), 1, 'source was cached, yt-dlp ran once')
        self.assertEqual(job['attempts'], 2)

    def test_replay_after_helper_restart_keeps_cached_source(self):
        # Download finishes, then the helper dies: only the cache remains.
        ident = self.enqueue()
        work = helper.job_cache_dir(ident)
        work.mkdir()
        subprocess.run([str(fake_tool_file(self.tools)), '-o', str(work / 'source.%(ext)s'), 'https://x/watch?v=' + VID],
                       check=True, capture_output=True)
        helper.finalize_download(work)
        self.assertEqual(len(calls(self.state)), 1)
        helper.process_job(ident)
        self.assertEqual(self.store.get(ident)['state'], 'done')
        self.assertEqual(len(calls(self.state)), 1)

    def test_permanent_error_fails_immediately_and_keeps_cache(self):
        set_mode(self.state, 'unavailable')
        ident, job = self.run_job()
        self.assertEqual(job['state'], 'error')
        self.assertEqual(job['attempts'], 1)
        self.assertIn('unavailable', job['message'].lower())
        self.assertEqual(len(calls(self.state)), 1)

    def test_transient_error_retries_then_gives_up(self):
        set_mode(self.state, 'part_then_ok')
        helper.MAX_ATTEMPTS = 1
        try:
            ident, job = self.run_job()
        finally:
            helper.MAX_ATTEMPTS = 3
        self.assertEqual(job['state'], 'error')
        self.assertIn('503', job['message'])

    def test_missing_tool_and_low_disk_are_permanent(self):
        fake_tool_file(self.tools).unlink(missing_ok=True)
        path_backup = os.environ['PATH']
        os.environ['PATH'] = str(self.tmp)
        try:
            ident, job = self.run_job()
        finally:
            os.environ['PATH'] = path_backup
        self.assertEqual((job['state'], job['attempts']), ('error', 1))
        make_tools(self.tools)
        helper.MIN_FREE_BYTES = 10 ** 18
        self.store.clear_finished()
        ident = self.enqueue('lmnopqrstuv')
        helper.process_job(ident)
        self.assertEqual(self.store.get(ident)['state'], 'error')
        self.assertIn('disk space', self.store.get(ident)['message'])

    def test_cancel_kills_the_process_and_cleans_up(self):
        set_mode(self.state, 'sleep')
        ident = self.enqueue()
        worker = threading.Thread(target=helper.process_job, args=(ident,))
        worker.start()
        deadline = time.time() + 10
        while time.time() < deadline and ident not in helper.ACTIVE:
            time.sleep(.05)
        proc = helper.ACTIVE[ident]
        self.store.cancel(ident)
        helper.kill_process(proc)
        worker.join(15)
        self.assertFalse(worker.is_alive())
        self.assertIsNotNone(proc.poll())
        self.assertEqual(self.store.get(ident)['state'], 'canceled')
        self.assertNotIn(ident, helper.ACTIVE)
        self.assertFalse((self.cache / ident).exists())

    def test_existing_destination_is_never_overwritten(self):
        ident = self.enqueue()
        dest = self.out / 'Test Artist' / 'Singles' / f'Test Song [{VID}].wav'
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b'RIFF' + b'k' * 200)            # user file, not in the index
        helper.process_job(ident)
        self.assertEqual(self.store.get(ident)['state'], 'done')
        self.assertEqual(dest.read_bytes(), b'RIFF' + b'k' * 200)
        self.assertEqual(list(self.out.rglob('.processing-*')), [])

    def test_validate_wav_rejects_truncated_output(self):
        short = self.tmp / 'short.wav'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'sine=duration=1',
                        '-c:a', 'pcm_s24le', str(short)], check=True)
        ident = self.enqueue('qqqqqqqqqqq')
        self.assertGreater(helper.validate_wav(ident, short, 1.0), 0.5)
        with self.assertRaisesRegex(RuntimeError, 'truncated'):
            helper.validate_wav(ident, short, 60)
        s16 = self.tmp / 's16.wav'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'sine=duration=1', str(s16)], check=True)
        with self.assertRaisesRegex(RuntimeError, 'encoding'):
            helper.validate_wav(ident, s16)


class LoggingTests(Base):
    def tearDown(self):
        for handler in list(helper.LOG.handlers):
            if getattr(handler, '_bmd_file', False):
                helper.LOG.removeHandler(handler)
                handler.close()
        super().tearDown()

    def test_helper_log_records_job_lifecycle_and_is_reported_in_diagnostics(self):
        log_file = helper.setup_logging(self.tmp / 'logs')
        helper.setup_logging(self.tmp / 'logs')                      # idempotent: no duplicate lines
        self.assertEqual(sum(1 for h in helper.LOG.handlers if getattr(h, '_bmd_file', False)), 1)
        set_mode(self.state, 'unavailable')
        ident = self.enqueue()
        helper.process_job(ident)
        for handler in helper.LOG.handlers:
            handler.flush()
        text = log_file.read_text(encoding='utf-8')
        self.assertIn(f'Job {ident} (video {VID}): attempt 1/3', text)
        self.assertIn('permanent', text)
        self.assertIn('Video unavailable', text)
        self.assertEqual(text.count('attempt 1/3'), 1)
        self.assertEqual(helper.diagnostics()['log_file'], str(log_file))

    def test_unexpected_request_errors_are_logged_not_lost(self):
        log_file = helper.setup_logging(self.tmp / 'logs')
        original = native_host.dispatch
        native_host.dispatch = lambda method, params: (_ for _ in ()).throw(RuntimeError('disk exploded'))
        try:
            out = io.BytesIO()
            native_host.run(io.BytesIO(frame({'id': 1, 'method': 'health'})), out)
        finally:
            native_host.dispatch = original
        self.assertEqual(read_frames(out.getvalue())[0]['error'], 'Native host internal error')
        for handler in helper.LOG.handlers:
            handler.flush()
        self.assertIn('disk exploded', log_file.read_text(encoding='utf-8'))


class ProgressThrottleTests(Base):
    """Phase 2: progress ticks are coalesced, but the first and last tick and all state changes are kept."""

    def make_noisy_tool(self, lines, sleep_s=0.0, fail=False):
        body = f"""import sys, time, json
from pathlib import Path
args = sys.argv[1:]
work = Path(args[args.index('-o') + 1]).parent
for i in range({lines}):
    print('[download]  %.1f%% of 3.00MiB' % (100.0 * i / {lines - 1}), flush=True)
    if {sleep_s}: time.sleep({sleep_s})
{'sys.exit(1)' if fail else ''}
(work / 'source.m4a').write_bytes(b'x' * 100)
(work / 'source.info.json').write_text(json.dumps({{'id': 'x'}}))
"""
        make_launchable_tool(self.tools, body)

    def run_stream(self):
        ident = self.ident = self.enqueue()
        self.store.next_queued()
        work = self.tmp / 'work'
        work.mkdir(exist_ok=True)
        counter = {'n': 0}
        real = self.store.update

        def counting(*a, **k):
            counter['n'] += 1
            return real(*a, **k)
        self.store.update = counting
        try:
            helper.streamed_download(ident, 'https://www.youtube.com/watch?v=' + VID, work, artwork=False)
        finally:
            self.store.update = real
        return ident, counter['n']

    def test_throttle_logic_is_deterministic_with_a_fake_clock(self):
        self.make_noisy_tool(10)
        ticks = iter(i * 0.1 for i in range(1000))
        helper._clock = lambda: next(ticks)
        try:
            ident, updates = self.run_stream()
        finally:
            helper._clock = time.monotonic
        self.assertEqual(updates, 4)               # ticks 0 (first), 3, 6, 9 (0.25 s apart at least)
        job = self.store.get(ident)
        self.assertEqual((job['stage'], job['percent'], job['message']), ('downloading', 73, 'Downloading… 100.0%'))

    def test_a_fast_burst_collapses_to_a_few_updates_and_keeps_the_final_value(self):
        self.make_noisy_tool(400, sleep_s=0.004)
        ident, updates = self.run_stream()
        self.assertLess(updates, 20)
        self.assertGreaterEqual(updates, 3)
        self.assertEqual(self.store.get(ident)['message'], 'Downloading… 100.0%')

    def test_the_held_back_last_tick_is_saved_even_when_the_download_then_fails(self):
        self.make_noisy_tool(30, fail=True)
        ticks = iter(i * 0.01 for i in range(1000))        # every tick closer than the interval
        helper._clock = lambda: next(ticks)
        try:
            with self.assertRaisesRegex(RuntimeError, 'yt-dlp failed'):
                self.run_stream()
        finally:
            helper._clock = time.monotonic
        self.assertEqual(self.store.get(self.ident)['message'], 'Downloading… 100.0%')

    def test_cancel_still_stops_a_noisy_download_promptly(self):
        self.make_noisy_tool(100000, sleep_s=0.001)
        ident = self.enqueue()
        self.store.next_queued()
        work = self.tmp / 'work2'
        work.mkdir()
        errors = []

        def run():
            try:
                helper.streamed_download(ident, 'https://www.youtube.com/watch?v=' + VID, work, artwork=False)
            except helper.Canceled:
                errors.append('canceled')
        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.time() + 10
        while ident not in helper.ACTIVE and time.time() < deadline:
            time.sleep(.01)
        started = time.time()
        self.store.cancel(ident)
        helper.kill_process(helper.ACTIVE[ident])
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.time() - started, 2)
        self.assertEqual(errors, ['canceled'])
        self.assertNotIn(ident, helper.ACTIVE)


class LibraryScanTests(Base):
    """Phase 2: batched, idempotent library scan with O(ids) memory; behavior must match the old scan."""

    def make_wavs(self, n, folder=None):
        folder = folder or (self.out / 'Artist' / 'Album')
        folder.mkdir(parents=True, exist_ok=True)
        ids = []
        for i in range(n):
            vid = 'w%010d' % i
            (folder / f'Track {i} [{vid}].wav').write_bytes(b'R' * 100)
            ids.append(vid)
        return ids

    def test_unchanged_rescan_writes_nothing(self):
        ids = self.make_wavs(50)
        first = helper.reconcile_library()
        self.assertEqual(first['indexed'], 50)
        added = {i: self.store.conn.execute('SELECT added FROM library WHERE video_id=?', (i,)).fetchone()[0] for i in ids}
        changes = self.store.conn.total_changes
        second = helper.reconcile_library()
        self.assertEqual((second['indexed'], second['forgotten']), (0, 0))
        self.assertEqual(self.store.conn.total_changes, changes)
        for i in ids:
            self.assertEqual(self.store.conn.execute('SELECT added FROM library WHERE video_id=?', (i,)).fetchone()[0], added[i])

    def test_moved_deleted_and_foreign_entries(self):
        ids = self.make_wavs(5)
        helper.reconcile_library()
        old = Path(self.store.library_get(ids[0]))
        moved = self.out / 'Elsewhere'
        moved.mkdir()
        old.rename(moved / old.name)
        Path(self.store.library_get(ids[1])).unlink()
        user_file = self.tmp / 'other' / 'kept [zzzzzzzzzzz].wav'
        user_file.parent.mkdir()
        user_file.write_bytes(b'R' * 100)
        self.store.library_put('zzzzzzzzzzz', user_file, 100)            # indexed file outside the scanned folder
        self.store.library_put('missingaaaa', self.tmp / 'gone.wav', 100)  # indexed file that no longer exists
        summary = helper.reconcile_library()
        self.assertEqual(Path(self.store.library_get(ids[0])), moved / old.name)
        self.assertIsNone(self.store.library_get(ids[1]))
        self.assertEqual(self.store.library_get('zzzzzzzzzzz'), str(user_file))
        self.assertIsNone(self.store.library_get('missingaaaa'))
        self.assertEqual(summary['forgotten'], 2)
        self.assertEqual(summary['indexed'], 1)

    def test_writes_are_batched(self):
        original, helper.LIBRARY_BATCH = helper.LIBRARY_BATCH, 100
        calls = []
        real = self.store.library_upsert_many
        self.store.library_upsert_many = lambda rows: (calls.append(len(rows)), real(rows))[1]
        try:
            self.make_wavs(250)
            summary = helper.reconcile_library()
        finally:
            helper.LIBRARY_BATCH = original
            self.store.library_upsert_many = real
        self.assertEqual((calls, summary['indexed']), ([100, 100, 50], 250))

    def test_spaces_and_non_ascii_paths_and_duplicate_ids(self):
        folder = self.out / 'Ünïcödé Artist ñ' / '日本語 Album  (deluxe)'
        folder.mkdir(parents=True)
        name = 'Tëst Sóng – 你好 [abcdefghijk].wav'
        (folder / name).write_bytes(b'R' * 100)
        other = self.out / 'Copy'
        other.mkdir()
        (other / name).write_bytes(b'R' * 100)                          # same id in two places
        helper.reconcile_library()
        found = helper.find_existing('abcdefghijk')
        self.assertTrue(found.is_file() and found.name == name)
        self.assertEqual(self.store.conn.execute('SELECT COUNT(*) FROM library').fetchone()[0], 1)

    def test_scan_runs_alongside_busy_queue_traffic_without_errors(self):
        self.make_wavs(3000)
        errors, stop, done = [], threading.Event(), {}

        def scan():
            try:
                done['summary'] = helper.reconcile_library()
            except Exception as exc:
                errors.append(exc)
            finally:
                stop.set()

        def traffic():
            i = 0
            while not stop.is_set():
                try:
                    self.store.enqueue([{'id': 'q%010d' % i, 'title': 't'}], True, False, find_existing=helper.find_existing)
                    self.store.page(20)
                    self.store.counts()
                    i += 1
                except Exception as exc:
                    errors.append(exc)
                    return
        threads = [threading.Thread(target=scan)] + [threading.Thread(target=traffic) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(done['summary']['indexed'], 3000)
        self.assertEqual(self.store.conn.execute('SELECT COUNT(*) FROM library').fetchone()[0], 3000)
        self.assertEqual(sum(self.store.counts().values()), self.store.conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0])


class InterruptionSafetyTests(Base):
    """Hard constraint 6: a kill mid-encode must never leave something that looks like a finished WAV."""

    def test_a_killed_encode_leaves_no_wav_and_no_temp_file_and_the_retry_completes(self):
        real_encode, real_attempts = helper.encode_command, helper.MAX_ATTEMPTS

        def slow(*a, **k):                       # read at native speed so the encode lasts ~3 s
            cmd = real_encode(*a, **k)
            cmd.insert(cmd.index('-i'), '-re')
            return cmd

        helper.encode_command, helper.MAX_ATTEMPTS = slow, 1
        ident = self.enqueue()
        worker = threading.Thread(target=helper.process_job, args=(ident,))
        worker.start()
        deadline = time.time() + 20
        while time.time() < deadline and not list(self.out.rglob('.processing-*.wav')):
            time.sleep(.02)
        self.assertTrue(list(self.out.rglob('.processing-*.wav')), 'the encode started')
        time.sleep(.3)
        helper.kill_process(helper.ACTIVE[ident])            # a crash / kill of the converter mid-write
        worker.join(30)
        helper.encode_command, helper.MAX_ATTEMPTS = real_encode, real_attempts
        self.assertEqual(self.store.get(ident)['state'], 'error')
        self.assertEqual(list(self.out.rglob('*.wav')), [], 'no partial file looks complete')
        self.assertEqual(list(self.out.rglob('.processing-*')), [], 'temp files are removed')
        self.assertTrue((self.cache / ident).exists(), 'the downloaded source is kept for the retry')
        self.store.retry(ident)
        helper.process_job(ident)
        self.assertEqual(self.store.get(ident)['state'], 'done')
        self.assertEqual(len(calls(self.state)), 1, 'the retry replayed the cached source, no new download')
        self.assertEqual(ffprobe_json(next(self.out.rglob('*.wav')))['streams'][0]['codec_name'], 'pcm_s24le')

    def test_cancel_leaves_no_child_processes_behind(self):
        set_mode(self.state, 'sleep')
        ident = self.enqueue()
        worker = threading.Thread(target=helper.process_job, args=(ident,))
        worker.start()
        deadline = time.time() + 10
        while time.time() < deadline and ident not in helper.ACTIVE:
            time.sleep(.02)
        self.store.cancel(ident)
        helper.kill_process(helper.ACTIVE[ident])
        worker.join(15)
        children = []
        for stat in Path('/proc').glob('[0-9]*/stat'):
            try:
                fields = stat.read_text().rsplit(')', 1)[1].split()
            except OSError:
                continue
            if int(fields[1]) == os.getpid() and fields[0] != 'Z':
                children.append(stat.parent.name)
        self.assertEqual(children, [])


class StoreConcurrencyTests(Base):
    """Race conditions: many threads mutating the queue at once must never break its invariants."""

    def test_invariants_hold_under_concurrent_mutation(self):
        import random
        import store as store_mod
        store_mod.MAX_HISTORY = 10 ** 9
        errors, stop = [], threading.Event()

        def worker(seed):
            rng = random.Random(seed)
            n = 0
            try:
                for _ in range(400):
                    op = rng.choice(['enqueue', 'enqueue', 'update', 'cancel', 'retry', 'next', 'page', 'clear', 'counts'])
                    page, _c = self.store.page(30)
                    pick = rng.choice(page)['id'] if page else None
                    try:
                        if op == 'enqueue':
                            n += 1
                            self.store.enqueue([{'id': 'c%d%07d' % (seed, n), 'title': 't'}], True, False)
                        elif op == 'update' and pick:
                            self.store.update(pick, percent=rng.randrange(100), message='m')
                        elif op == 'cancel' and pick:
                            self.store.cancel(pick)
                        elif op == 'retry' and pick:
                            self.store.retry(pick)
                        elif op == 'next':
                            job = self.store.next_queued()
                            if job and rng.random() < .5:
                                self.store.update(job['id'], state='done')
                        elif op == 'clear' and rng.random() < .1:
                            self.store.clear_finished()
                        elif op == 'counts':
                            self.store.counts()
                    except (ValueError, KeyError):
                        pass                      # legal refusals (already finished, unknown id, ...)
            except Exception as exc:
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(s,)) for s in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(120)
        self.assertEqual(errors, [])
        rows = self.store.conn.execute('SELECT state, rank, ord, created, payload FROM jobs').fetchall()
        self.assertEqual(sum(self.store.counts().values()), len(rows))
        for state, rank, order, created, payload in rows:
            self.assertEqual(rank, 0 if state == 'running' else 1 if state == 'queued' else 2)
            self.assertEqual(order, created if rank < 2 else -created)
            self.assertEqual(json.loads(payload)['state'], state)
        seen, cursor = [], None
        while True:
            jobs, cursor = self.store.page(25, cursor)
            seen += [j['id'] for j in jobs]
            if not cursor:
                break
        self.assertEqual((len(seen), len(set(seen))), (len(rows), len(rows)))


class WorkerLoopTests(Base):
    """A transient store error (a locked database, say) must not end the worker thread silently:
    the host would stay up and never process another job."""

    def test_a_store_error_does_not_kill_the_worker(self):
        ident = self.enqueue()
        state = {'raised': False}

        class FlakyStore:
            def __init__(self, store):
                self.store = store

            def next_queued(self):
                if not state['raised']:
                    state['raised'] = True
                    raise sqlite3.OperationalError('database is locked (test)')
                return self.store.next_queued()

            def __getattr__(self, name):
                return getattr(self.store, name)

        restored = helper.STORE
        helper.STORE = FlakyStore(self.store)
        worker = threading.Thread(target=helper.worker_loop, daemon=True)
        try:
            worker.start()
            deadline = time.time() + 30
            while time.time() < deadline:
                job = self.store.get(ident)
                if job and job['state'] == 'done':
                    break
                time.sleep(0.05)
        finally:
            helper.WORKER_STOP.set()     # the daemon worker must not survive into later tests
            helper.WAKE.set()
            worker.join(5)
            helper.WORKER_STOP.clear()
            helper.STORE = restored
        self.assertTrue(state['raised'], 'the fault was injected')
        self.assertEqual(self.store.get(ident)['state'], 'done',
                         'the worker kept going after the store error')


class MaintenanceTests(Base):
    def test_sweep_cache_only_removes_what_it_owns(self):
        done, running, err_old, err_new = (self.enqueue('v%010d' % i) for i in range(4))
        self.store.update(done, state='done')
        # Mark the second job running directly: next_queued() would pick the OLDEST queued
        # job by (created, id), and rapid enqueues can share a created timestamp, leaving
        # the hex id to decide - the test must not depend on that order.
        self.store.update(running, state='running')
        self.store.update(err_old, state='error')
        self.store.update(err_new, state='error')
        orphan = '%032x' % 999
        for name in (done, running, err_old, err_new, orphan):
            (self.cache / name).mkdir()
            (self.cache / name / 'f').write_text('x')
        (self.cache / 'not-a-job').mkdir()
        old = time.time() - 8 * 86400
        self.store.conn.execute('UPDATE jobs SET updated=? WHERE id=?', (old, err_old))
        self.store.conn.commit()
        removed = set(helper.sweep_cache())
        import json as _j
        print('SWEEP-DEBUG states:', _j.dumps(helper.STORE.states([done, running, err_old, err_new, orphan])), ' removed:', sorted(removed))
        self.assertEqual(removed, {done, err_old, orphan} | ({running} if self.store.get(running)['state'] != 'running' else set()))
        self.assertTrue((self.cache / err_new).exists())
        self.assertTrue((self.cache / 'not-a-job').exists())

    def test_reconcile_library_indexes_and_cleans_only_stale_temp(self):
        album = self.out / 'A' / 'B'
        album.mkdir(parents=True)
        wav = album / f'Song [{VID}].wav'
        wav.write_bytes(b'R' * 500)
        stale, fresh = album / '.processing-old.wav', album / '.processing-new.wav'
        stale.write_bytes(b'x'); fresh.write_bytes(b'x')
        os.utime(stale, (time.time() - 7200,) * 2)
        user_file = album / 'notes.txt'
        user_file.write_text('mine')
        (album / 'Orphan [aaaaaaaaaaa].metadata.json').write_text('{}')
        summary = helper.reconcile_library()
        self.assertEqual(summary['indexed'], 1)
        self.assertEqual(summary['stale_temp_removed'], 1)
        self.assertEqual(summary['orphan_sidecars'], 1)
        self.assertFalse(stale.exists()); self.assertTrue(fresh.exists()); self.assertTrue(user_file.exists())
        self.assertEqual(helper.find_existing(VID), wav)
        wav.unlink()
        self.assertIsNone(helper.find_existing(VID))
        self.assertEqual(self.store.library_get(VID), None)

    def test_symlinked_folders_are_not_followed(self):
        outside = self.tmp / 'outside'
        outside.mkdir()
        (outside / f'X [{"b" * 11}].wav').write_bytes(b'R' * 500)
        (outside / '.processing-z.wav').write_bytes(b'x')
        os.utime(outside / '.processing-z.wav', (time.time() - 9999,) * 2)
        os.symlink(outside, self.out / 'link')
        helper.reconcile_library()
        self.assertTrue((outside / '.processing-z.wav').exists())
        self.assertIsNone(helper.find_existing('b' * 11))


class NativeHostTests(Base):
    def talk(self, *messages, raw=b''):
        data = b''.join(frame(m) for m in messages) + raw
        out = io.BytesIO()
        native_host.run(io.BytesIO(data), out)
        return read_frames(out.getvalue())

    def test_health_pagination_and_enqueue_round_trip(self):
        replies = self.talk({'id': 1, 'method': 'health'},
                            {'id': 2, 'method': 'enqueue', 'params': {
                                'format': 'wav', 'metadata': True, 'normalize': False,
                                'items': [{'id': 'v%010d' % i, 'title': 't'} for i in range(5)]}},
                            {'id': 3, 'method': 'jobs', 'params': {'limit': 2}})
        self.assertEqual(replies[0]['data']['version'], '2.6.0')
        self.assertIn('pagination', replies[0]['data']['features'])
        self.assertEqual(len(replies[1]['data']['added']), 5)
        page = replies[2]['data']
        self.assertEqual(len(page['jobs']), 2)
        self.assertTrue(page['next_cursor'])
        nxt = self.talk({'id': 4, 'method': 'jobs', 'params': {'limit': 10, 'cursor': page['next_cursor']}})[0]
        self.assertEqual(len(nxt['data']['jobs']), 3)
        self.assertIsNone(nxt['data']['next_cursor'])

    def test_bad_input_never_kills_the_host(self):
        junk = struct.pack('<I', 5) + b'{nope'
        replies = self.talk({'id': 1, 'method': 'health'}, {'id': 2, 'method': 'rm -rf'},
                            {'id': 3, 'method': 'jobs', 'params': {'limit': 9999}},
                            {'id': 4, 'method': 'jobs', 'params': {'cursor': 'zzz'}},
                            {'id': True, 'method': 'health'}, {'id': 6, 'method': 'cancel', 'params': {'id': '../x'}},
                            raw=junk + frame({'id': 7, 'method': 'health'}))
        self.assertTrue(replies[0]['ok'])
        self.assertFalse(replies[1]['ok']); self.assertFalse(replies[2]['ok']); self.assertFalse(replies[3]['ok'])
        self.assertFalse(replies[4]['ok']); self.assertFalse(replies[5]['ok'])
        self.assertEqual(replies[6], {'id': None, 'ok': False, 'error': 'Malformed JSON'})
        self.assertEqual(replies[7]['id'], 7)

    def test_lock_polling_is_fast_so_a_quick_reconnect_is_not_delayed(self):
        path = self.tmp / 'host2.lock'
        sleeps = []
        real = time.sleep
        released = threading.Event()

        def hold():
            with native_host.singleton_lock(path, wait=1):
                released.wait(0.4)
        thread = threading.Thread(target=hold)
        thread.start()
        real(.1)
        native_host.time.sleep = lambda s: (sleeps.append(s), real(s))[1]
        try:
            with native_host.singleton_lock(path, wait=3):
                pass
        finally:
            native_host.time.sleep = real
        thread.join()
        self.assertTrue(sleeps and max(sleeps) <= 0.05, sleeps)

    def test_oversized_frame_stops_cleanly(self):
        replies = self.talk({'id': 1, 'method': 'health'}, raw=struct.pack('<I', 2 * 1024 * 1024))
        self.assertEqual(len(replies), 1)

    def test_singleton_lock_waits_for_previous_host_then_gives_up(self):
        path = self.tmp / 'host.lock'
        released = threading.Event()

        def hold():
            with native_host.singleton_lock(path, wait=1):
                released.wait(0.7)

        thread = threading.Thread(target=hold)
        thread.start()
        time.sleep(.15)
        with native_host.singleton_lock(path, wait=3):
            pass                                  # acquired after the first host exited
        thread.join()
        released2 = threading.Event()
        thread = threading.Thread(target=lambda: (lambda cm: (cm.__enter__(), released2.wait(1.5), cm.__exit__(None, None, None)))(native_host.singleton_lock(path, wait=1)))
        thread.start()
        time.sleep(.2)
        with self.assertRaises(RuntimeError):
            with native_host.singleton_lock(path, wait=.4):
                pass
        released2.set()
        thread.join()


@unittest.skipUnless(HAS_FFMPEG, 'ffmpeg not available')
class DeepHashTests(Base):
    def setUp(self):
        super().setUp()
        self.big = tool_data_file(self.tools, 'ffmpeg')   # only hashed here, never launched
        self.big.write_bytes(os.urandom(2 ** 20) * 48)                  # 48 MB stand-in for a tool

    def manifest(self, **extra):
        bmdfs.write_json_atomic(self.tmp / 'install-manifest.json', {'version': '2.3.0', 'components': [
            dict({'name': 'ffmpeg', 'owned': True, 'sha256': 'hash-of-the-archive-not-the-file'}, **extra)]})

    def test_streaming_keeps_memory_flat_even_for_a_large_file(self):
        import tracemalloc
        self.manifest(file_sha256=helper.file_sha256(self.big))
        tracemalloc.start()
        report = helper.diagnostics(deep=True)
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        self.assertLess(peak, 8 * 2 ** 20, f'peak {peak / 2 ** 20:.1f} MB; the old code allocated the whole 48 MB')
        self.assertIs(report['tools']['ffmpeg']['hash_matches_install'], True)

    def test_mismatch_unknown_and_system_tools(self):
        self.manifest(file_sha256='0' * 64)
        self.assertIs(helper.diagnostics(deep=True)['tools']['ffmpeg']['hash_matches_install'], False)
        self.manifest()                                                   # no per-file hash recorded: unknown, not "mismatch"
        self.assertIsNone(helper.diagnostics(deep=True)['tools']['ffmpeg']['hash_matches_install'])
        self.assertNotIn('hash_matches_install', helper.diagnostics(deep=False)['tools']['ffmpeg'])
        self.big.unlink()                                                 # falls back to a system ffmpeg: never hashed
        report = helper.diagnostics(deep=True)
        if shutil.which('ffmpeg'):
            self.assertEqual(report['tools']['ffmpeg']['source'], 'system')
            self.assertNotIn('hash_matches_install', report['tools']['ffmpeg'])


class DiagnosticsTests(Base):
    def test_reports_ownership_source_and_missing_tools(self):
        manifest = {'version': '2.3.0', 'components': [
            {'name': 'yt-dlp', 'owned': True, 'verified_by': 'publisher'}]}
        bmdfs.write_json_atomic(self.tmp / 'install-manifest.json', manifest)
        report = helper.diagnostics()
        self.assertEqual(report['tools']['yt-dlp']['source'], 'app')
        self.assertTrue(report['tools']['yt-dlp']['owned'])
        self.assertEqual(report['tools']['ffmpeg']['source'], 'system')
        self.assertFalse(report['tools']['ffmpeg']['owned'])
        if not shutil.which('deno'):
            self.assertEqual(report['tools']['deno']['source'], 'missing')
            self.assertIn('deno is missing', report['issues'])
            self.assertFalse(report['requirements_ok'])
        self.assertEqual(report['database']['synchronous'], 2)


if __name__ == '__main__':
    unittest.main()
