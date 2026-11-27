#!/usr/bin/env python3
"""Benchmark harness for Brave Music Downloader 2.3.0 (Phase 1: measures only, changes no app code).

  python bench/run.py                    # everything (several minutes)
  python bench/run.py store host         # selected sections
  python bench/run.py --quick            # smaller sizes
Sections: env host nm reconnect store library pipeline cancel misc installer
"""
import cProfile
import hashlib
import json
import os
import pstats
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from common import (HELPER, ROOT, FAKE_YTDLP, Host, child_usage, env_info, frame, host_env, make_db, now_ms,
                    proc_stats, read_frame, run_cmd, stats, tmpdir)

CACHE = Path(__file__).resolve().parent / '.cache'


def sh(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


# ----------------------------------------------------------------------------- fixtures
def fixtures(seconds_list=(180, 3600)):
    CACHE.mkdir(exist_ok=True)
    out = {}
    for seconds in seconds_list:
        path = CACHE / f'music-{seconds}s.m4a'
        if not path.exists():
            sh(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'sine=frequency=220:sample_rate=44100:duration={seconds}',
                '-f', 'lavfi', '-i', f'anoisesrc=color=pink:amplitude=0.15:sample_rate=44100:duration={seconds}',
                '-filter_complex', '[0:a][1:a]amix=inputs=2,tremolo=f=0.4:d=0.5,aformat=channel_layouts=stereo',
                '-c:a', 'aac', '-b:a', '128k', str(path)], check=True)
        Path(str(path) + '.info.json').write_text(json.dumps(
            {'id': 'x', 'title': 'Bench Song', 'uploader': 'Bench Artist', 'duration': float(seconds),
             'upload_date': '20240101', 'acodec': 'aac'}))
        thumb = Path(str(path) + '.jpg')
        if not thumb.exists():
            sh(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc=s=640x360', '-frames:v', '1', str(thumb)], check=True)
        out[seconds] = path
    return out


def make_tools(folder):
    folder.mkdir(parents=True, exist_ok=True)
    stub = folder / 'yt-dlp'
    stub.write_text(FAKE_YTDLP.format(python=sys.executable))
    stub.chmod(0o755)
    return folder


# ----------------------------------------------------------------------------- sections
def sec_env(quick):
    return env_info()


def sec_host(quick):
    base = tmpdir()
    hdir = base / 'helper'
    shutil.copytree(HELPER, hdir, ignore=shutil.ignore_patterns('__pycache__'))
    env = host_env(base)
    res = {}
    res['interpreter_floor_ms'] = stats([run_cmd([sys.executable, '-c', 'pass'])[0] for _ in range(15)])
    code = f"import sys; sys.path.insert(0, {str(hdir)!r}); import native_host"
    _, run = run_cmd([sys.executable, '-X', 'importtime', '-c', code], env=env)
    rows = []
    for line in run.stderr.splitlines():
        m = re.match(r'import time:\s+(\d+) \|\s+(\d+) \| (\s*)(\S+)', line)
        if m:
            rows.append((int(m.group(1)), int(m.group(2)), m.group(4)))
    total = next((c for s, c, n in rows if n == 'native_host'), None)
    res['import_native_host_ms'] = round(total / 1000, 1) if total else None
    res['import_top_self_ms'] = [(n, round(s / 1000, 2)) for s, c, n in sorted(rows, reverse=True)[:10]]
    cold = []
    for _ in range(5):
        for p in hdir.rglob('__pycache__'):
            shutil.rmtree(p, ignore_errors=True)
        for d in ('data', 'root', 'music'):
            shutil.rmtree(base / d, ignore_errors=True)
        h = Host(hdir, env)
        h.request('health')
        cold.append(now_ms() - h.t0)
        h.close()
    res['cold_spawn_to_first_reply_ms'] = stats(cold)
    warm = []
    for _ in range(15):
        h = Host(hdir, env)
        h.request('health')
        warm.append(now_ms() - h.t0)
        h.close()
    res['warm_spawn_to_first_reply_ms'] = stats(warm)
    for rows_n in ((10000,) if quick else (10000, 100000)):
        for suffix in ('', '-wal', '-shm'):
            (base / 'data' / ('queue.sqlite3' + suffix)).unlink(missing_ok=True)
        make_db(base / 'data' / 'queue.sqlite3', rows_n)
        samples = []
        for _ in range(8):
            h = Host(hdir, env)
            h.request('health')
            samples.append(now_ms() - h.t0)
            h.close()
        res[f'warm_with_{rows_n}_row_db_ms'] = stats(samples)
    shutil.rmtree(base, ignore_errors=True)
    return res


def sec_nm(quick):
    base = tmpdir()
    hdir = base / 'helper'
    shutil.copytree(HELPER, hdir, ignore=shutil.ignore_patterns('__pycache__'))
    env = host_env(base)
    make_db(base / 'data' / 'queue.sqlite3', 10000)
    h = Host(hdir, env)
    h.request('health')
    n = 150 if quick else 300
    health = [h.request('health')[0] for _ in range(n)]
    jobs, sizes = [], []
    for _ in range(n):
        lat, reply, size = h.request('jobs', {'limit': 50})
        jobs.append(lat)
        sizes.append(size)
    small = [h.request('jobs', {'limit': 6})[0] for _ in range(n)]
    counts_only = [h.request('jobs', {'limit': 1})[0] for _ in range(n)]
    t = now_ms()
    for _ in range(n):
        h.send('jobs', {'limit': 50})
    h.proc.stdin.flush()
    for _ in range(n):
        read_frame(h.proc.stdout)
    piped = now_ms() - t
    cpu, rss = proc_stats(h.proc.pid)
    h.close()
    shutil.rmtree(base, ignore_errors=True)
    return {'health_rtt_ms': stats(health), 'jobs50_rtt_ms': stats(jobs), 'jobs6_rtt_ms': stats(small),
            'jobs1_rtt_ms': stats(counts_only), 'jobs50_reply_bytes': sizes[0],
            'pipelined_jobs50_req_per_s': round(n / (piped / 1000), 1), 'host_cpu_s_total': cpu, 'host_peak_rss_mb': rss,
            'requests': n * 4 + 1 + n}


def sec_reconnect(quick):
    base = tmpdir()
    hdir = base / 'helper'
    shutil.copytree(HELPER, hdir, ignore=shutil.ignore_patterns('__pycache__'))
    env = host_env(base)
    res = {}
    for mode in ('graceful_exit', 'killed'):
        waits, exits = [], []
        for _ in range(6 if quick else 10):
            a = Host(hdir, env)
            a.request('health')
            b = Host(hdir, env)            # the browser starts the new helper while the old one is going away
            time.sleep(random.uniform(.55, .85))   # random phase against the 200 ms lock-poll cycle
            t = now_ms()
            if mode == 'graceful_exit':
                a.proc.stdin.close()
                a.proc.wait(10)
            else:
                a.proc.kill()
                a.proc.wait(10)
            exits.append(now_ms() - t)
            b.request('health')
            waits.append(now_ms() - t)
            b.close()
            a.proc.stdout.close(); a.proc.stderr.close()
        res[mode] = {'old_helper_exit_ms': stats(exits), 'until_new_helper_answers_ms': stats(waits)}
    res['lock_poll_interval_ms'] = 200
    res['extension_fixed_pause_before_retry_ms'] = 350
    shutil.rmtree(base, ignore_errors=True)
    return res


def sec_store(quick):
    sys.path.insert(0, str(HELPER))
    import store as store_mod
    base = tmpdir()
    res = {}
    for n in ((1000, 10000) if quick else (1000, 10000, 100000)):
        path = base / f'q{n}.sqlite3'
        size = make_db(path, n)
        r = {'db_mb': round(size / 2 ** 20, 2)}
        t = []
        for _ in range(5):
            s0 = now_ms()
            s = store_mod.Store(path)
            t.append(now_ms() - s0)
            s.close()
        r['open_ms'] = stats(t)
        s = store_mod.Store(path)
        r['page50_first_ms'] = stats([_time(lambda: s.page(50)) for _ in range(40)])
        cursors, cursor, t0 = [], None, now_ms()
        pages = 0
        while True:
            rows, cursor = s.page(200, cursor)
            pages += 1
            if not cursor:
                break
            cursors.append(cursor)
        r['walk_all_pages200_total_ms'] = round(now_ms() - t0, 1)
        r['walk_pages'] = pages
        for label, frac in (('page50_at_50pct_ms', .5), ('page50_at_95pct_ms', .95)):
            c = cursors[min(len(cursors) - 1, int(len(cursors) * frac))]
            r[label] = stats([_time(lambda: s.page(50, c)) for _ in range(30)])
        r['page50_state_done_ms'] = stats([_time(lambda: s.page(50, None, 'done')) for _ in range(30)])
        r['page50_active_ms'] = stats([_time(lambda: s.page(50, None, 'active')) for _ in range(30)])
        r['counts_ms'] = stats([_time(s.counts) for _ in range(30)])
        first, _ = s.page(100, None, 'error')
        ids = [j['id'] for j in first]
        r['states_100_ids_ms'] = stats([_time(lambda: s.states(ids)) for _ in range(20)])
        r['get_ms'] = stats([_time(lambda: s.get(random.choice(ids))) for _ in range(200)])
        queued, _ = s.page(5, None, 'queued')
        qid = queued[0]['id']
        r['update_progress_ms'] = stats([_time(lambda i=i: s.update(qid, percent=i % 100, message='Downloading… %d%%' % (i % 100))) for i in range(300)])
        prof = cProfile.Profile()
        prof.runcall(lambda: [s.update(qid, percent=i % 100, message='x') for i in range(500)])
        st = pstats.Stats(prof)
        total = st.total_tt
        get = lambda name: round(sum(v[3] for k, v in st.stats.items() if name in k[2]) / 500 * 1000, 4)
        r['update_profile_ms_per_call'] = {'total': round(total / 500 * 1000, 4), 'sqlite_commit': get('commit'),
                                          'json_dumps': get('dumps'), 'json_loads': get('loads'), 'execute': get('execute')}
        items = [{'id': 'e%010d' % i, 'title': 't'} for i in range(200)]
        try:
            r['enqueue_200_ms'] = round(_time(lambda: s.enqueue(items, True, True, find_existing=None)), 2)
            r['enqueue_blocked_by_history_limit'] = False
        except ValueError as exc:               # a real product limit: MAX_HISTORY = 5000 rows, no automatic pruning
            r['enqueue_blocked_by_history_limit'] = str(exc)
            store_mod.MAX_HISTORY, saved = 10 ** 9, store_mod.MAX_HISTORY
            r['enqueue_200_ms_with_limit_lifted'] = round(_time(lambda: s.enqueue(items, True, True, find_existing=None)), 2)
            store_mod.MAX_HISTORY = saved
        r['library_put_commit_each_x1000_ms'] = round(_time(lambda: [s.library_put('l%010d' % i, '/m/a%d.wav' % i, 100) for i in range(1000)]), 1)

        def bulk():
            with s.lock:
                s.conn.executemany('INSERT OR REPLACE INTO library VALUES (?,?,?,?)',
                                   [('m%010d' % i, '/m/a%d.wav' % i, 100, 0.0) for i in range(1000)])
                s.conn.commit()
        r['library_put_one_transaction_x1000_ms'] = round(_time(bulk), 1)
        r['reference_payload_LIKE_scan_ms'] = round(_time(lambda: s.conn.execute("SELECT count(*) FROM jobs WHERE payload LIKE '%ZZZnomatch%'").fetchone()), 1)
        r['clear_finished_ms'] = round(_time(s.clear_finished), 1)
        r['wal_mb_after'] = round(Path(str(path) + '-wal').stat().st_size / 2 ** 20, 2) if Path(str(path) + '-wal').exists() else 0
        s.close()
        res[str(n)] = r
    shutil.rmtree(base, ignore_errors=True)
    return res


def _time(fn):
    t = now_ms()
    fn()
    return now_ms() - t


def child_library(n, folder):
    sys.path.insert(0, str(HELPER))
    import helper
    folder = Path(folder)
    music = folder / 'music'
    t0 = time.time()
    per = 12
    for i in range(n):
        d = music / f'Artist {i // (per * 8):05d}' / f'Album {(i // per) % 8}'
        if i % per == 0:
            d.mkdir(parents=True, exist_ok=True)
        (d / f'Track {i} [v{i:010d}].wav').write_bytes(b'R' * 100)
    created = time.time() - t0
    helper.configure(output=music, job_cache=folder / 'cache', mb_cache=folder / 'mb', tools_dir=folder / 'tools')
    store = helper.open_store(folder / 'q.sqlite3')
    commits = {'n': 0}
    store.conn.set_trace_callback(lambda sql: commits.__setitem__('n', commits['n'] + (1 if sql.strip().upper().startswith('COMMIT') else 0)))
    before = child_usage()
    t = now_ms()
    first = helper.reconcile_library()
    first_ms = now_ms() - t
    mid = child_usage()
    t = now_ms()
    second = helper.reconcile_library()
    second_ms = now_ms() - t
    ids = ['v%010d' % random.randrange(n) for _ in range(1000)]
    t = now_ms()
    for i in ids:
        helper.find_existing(i)
    lookup = now_ms() - t
    return {'files': n, 'create_files_s': round(created, 1), 'first_reconcile_ms': round(first_ms, 1),
            'first_reconcile_commits': commits['n'], 'first_summary': first, 'second_reconcile_ms': round(second_ms, 1),
            'second_summary': second, 'find_existing_1000_ms': round(lookup, 1),
            'peak_rss_mb': round(mid['self_maxrss_mb'], 1), 'rss_before_mb': round(before['self_maxrss_mb'], 1)}


def sec_library(quick):
    res = {}
    for n in ((1000, 10000) if quick else (1000, 10000, 100000)):
        d = tmpdir()
        run = sh([sys.executable, __file__, '--child', 'library', str(n), str(d)], timeout=1800)
        shutil.rmtree(d, ignore_errors=True)
        res[str(n)] = json.loads(run.stdout.strip().splitlines()[-1]) if run.returncode == 0 else {'error': run.stderr[-400:]}
    return res


# ----------------------------------------------------------------------------- pipeline
def classify(args):
    exe = os.path.basename(args[0]).split('.')[0]
    text = ' '.join(map(str, args))
    if exe == 'ffprobe':
        return 'ffprobe:validate' if 'codec_name' in text else 'ffprobe:sample-rate'
    if exe == 'ffmpeg':
        if 'print_format=json' in text:
            return 'ffmpeg:loudnorm-analysis'
        if '-frames:v' in text:
            return 'ffmpeg:thumbnail'
        if 'pcm_f32le' in text:
            return 'ffmpeg:trim-prepass'
        return 'ffmpeg:encode-wav'
    return exe


def child_pipeline(scenario, folder, fixture_dir):
    sys.path.insert(0, str(HELPER))
    import bmdfs
    import helper
    import subprocess as sp
    folder = Path(folder)
    spec = {'short': (180, 1, True, False), 'short-nonorm': (180, 1, False, False), 'short-trim': (180, 1, True, True),
            'playlist20': (180, 20, True, False), 'long1h': (3600, 1, True, False), 'long1h-nonorm': (3600, 1, False, False)}[scenario]
    seconds, count, normalize, trim = spec
    fixture = Path(fixture_dir) / f'music-{seconds}s.m4a'
    os.environ['BENCH_FIXTURE'] = str(fixture)
    tools = make_tools(folder / 'tools')
    helper.configure(output=folder / 'music', job_cache=folder / 'cache' / 'jobs', mb_cache=folder / 'mb', tools_dir=tools)
    helper.MIN_FREE_BYTES = 1
    store = helper.open_store(folder / 'q.sqlite3')
    stages, spawns = [], {'n': 0}

    class CountingPopen(sp.Popen):
        def __init__(self, *a, **k):
            spawns['n'] += 1
            super().__init__(*a, **k)
    sp.Popen = CountingPopen

    def timed(label_fn, fn):
        def wrapper(*a, **k):
            t = now_ms()
            try:
                return fn(*a, **k)
            finally:
                stages.append((label_fn(a), now_ms() - t))
        return wrapper
    helper.command = timed(lambda a: classify(a[1]), helper.command)
    helper.streamed_download = timed(lambda a: 'yt-dlp(stub: copy + progress lines)', helper.streamed_download)
    helper.finalize_download = timed(lambda a: 'finalize-download', helper.finalize_download)
    bmdfs.fsync_file = timed(lambda a: 'fsync-wav', bmdfs.fsync_file)
    bmdfs.commit_new_file = timed(lambda a: 'atomic-commit', bmdfs.commit_new_file)
    updates = {'n': 0, 'ms': 0.0}
    real_update = store.update

    def update(*a, **k):
        t = now_ms()
        try:
            return real_update(*a, **k)
        finally:
            updates['n'] += 1
            updates['ms'] += now_ms() - t
    store.update = update
    peak = {'mb': 0.0}
    stop = threading.Event()

    def sampler():
        while not stop.wait(.1):
            total = bmdfs.dir_size(folder / 'cache') + bmdfs.dir_size(folder / 'music')
            peak['mb'] = max(peak['mb'], total / 2 ** 20)
    threading.Thread(target=sampler, daemon=True).start()
    ids = []
    for i in range(count):
        r = store.enqueue([{'id': 'b%010d' % i, 'title': 'T'}], True, normalize, musicbrainz=False, artwork=True,
                          organize=True, trim_start=trim, find_existing=None)
        ids.append(r['added'][0])
    before = child_usage()
    wall0 = now_ms()
    per_job = []
    for ident in ids:
        t = now_ms()
        helper.process_job(ident)
        per_job.append(now_ms() - t)
    wall = now_ms() - wall0
    after = child_usage()
    stop.set()
    states = [store.get(i)['state'] for i in ids]
    wavs = [p.stat().st_size for p in (folder / 'music').rglob('*.wav')]
    agg = {}
    for label, ms in stages:
        a = agg.setdefault(label, {'calls': 0, 'ms': 0.0})
        a['calls'] += 1
        a['ms'] = round(a['ms'] + ms, 1)
    cpu = (after['child_cpu_s'] - before['child_cpu_s']) + (after['self_cpu_s'] - before['self_cpu_s'])
    return {'scenario': scenario, 'audio_seconds': seconds, 'jobs': count, 'all_done': set(states) == {'done'},
            'wall_s': round(wall / 1000, 2), 'per_job_s': round(sum(per_job) / len(per_job) / 1000, 2),
            'realtime_factor': round(seconds * count / (wall / 1000), 1), 'cpu_s': round(cpu, 2),
            'cpu_utilisation': round(cpu / (wall / 1000), 2), 'processes_spawned': spawns['n'],
            'processes_per_job': round(spawns['n'] / count, 1), 'stage_ms_totals': agg,
            'store_updates': updates['n'], 'store_update_total_ms': round(updates['ms'], 1),
            'ffmpeg_peak_rss_mb': round(after['child_maxrss_mb'], 1), 'python_peak_rss_mb': round(after['self_maxrss_mb'], 1),
            'peak_disk_mb': round(peak['mb'], 1), 'final_wav_mb': round(sum(wavs) / 2 ** 20, 1),
            'block_out_mb': round(after['block_out_mb'] - before['block_out_mb'], 1)}


def sec_pipeline(quick):
    fx = fixtures((180,) if quick else (180, 3600))
    res = {'spawn_latency_ms': {}}
    for name, cmd in (('python -c pass', [sys.executable, '-c', 'pass']), ('ffprobe -version', ['ffprobe', '-version']),
                      ('ffmpeg -version', ['ffmpeg', '-version'])):
        res['spawn_latency_ms'][name] = stats([run_cmd(cmd)[0] for _ in range(20)])
    scenarios = ['short', 'short-nonorm', 'short-trim', 'playlist20'] + ([] if quick else ['long1h', 'long1h-nonorm'])
    if os.environ.get('BENCH_SCENARIOS'):
        scenarios = os.environ['BENCH_SCENARIOS'].split(',')
    for sc in scenarios:
        d = tmpdir()
        run = sh([sys.executable, __file__, '--child', 'pipeline', sc, str(d), str(CACHE)], timeout=3600)
        shutil.rmtree(d, ignore_errors=True)
        res[sc] = json.loads(run.stdout.strip().splitlines()[-1]) if run.returncode == 0 else {'error': (run.stderr or run.stdout)[-600:]}
    return res


def child_cancel(kind, folder, fixture_dir):
    sys.path.insert(0, str(HELPER))
    import helper
    folder = Path(folder)
    seconds = 180
    os.environ['BENCH_FIXTURE'] = str(Path(fixture_dir) / f'music-{seconds}s.m4a')
    os.environ['BENCH_MODE'] = 'sleep' if kind == 'download' else 'copy'
    kind = 'download' if kind == 'download' else 'analysis'
    tools = make_tools(folder / 'tools')
    helper.configure(output=folder / 'music', job_cache=folder / 'cache' / 'jobs', mb_cache=folder / 'mb', tools_dir=tools)
    helper.MIN_FREE_BYTES = 1
    store = helper.open_store(folder / 'q.sqlite3')
    ident = store.enqueue([{'id': 'c0000000000', 'title': 'T'}], True, True, musicbrainz=False, find_existing=None)['added'][0]
    worker = threading.Thread(target=helper.process_job, args=(ident,))
    worker.start()
    deadline = time.time() + 120
    target = 'downloading' if kind == 'download' else 'analyzing'
    while time.time() < deadline:
        job = store.get(ident)
        if job['stage'] == target and ident in helper.ACTIVE:
            break
        time.sleep(.02)
    time.sleep(.5)
    proc = helper.ACTIVE.get(ident)
    t = now_ms()
    store.cancel(ident)                    # what the native host does on a cancel request
    helper.kill_process(proc)
    killed = None
    while proc.poll() is None and now_ms() - t < 30000:
        time.sleep(.001)
    killed = now_ms() - t
    worker.join(30)
    returned = now_ms() - t
    kids = sh(['pgrep', '-P', str(os.getpid())]).stdout.split()
    cache_left = (folder / 'cache' / 'jobs' / ident).exists()
    temp_left = [p.name for p in (folder / 'music').rglob('.processing-*')]
    return {'kind': kind, 'process_dead_ms': round(killed, 1), 'worker_returned_ms': round(returned, 1),
            'state': store.get(ident)['state'], 'orphan_children': len(kids), 'cache_dir_left': cache_left,
            'temp_files_left': temp_left}


def sec_cancel(quick):
    fx = fixtures((180,) if quick else (180, 3600))
    res = {}
    for kind in ('download', 'analysis'):
        out = []
        for _ in range(5):
            d = tmpdir()
            run = sh([sys.executable, __file__, '--child', 'cancel', kind, str(d), str(CACHE)], timeout=900)
            shutil.rmtree(d, ignore_errors=True)
            out.append(json.loads(run.stdout.strip().splitlines()[-1]) if run.returncode == 0 else {'error': run.stderr[-300:]})
        res[kind] = out
    res['retry_backoff_seconds'] = [5, 15]
    return res


def child_deephash(folder):
    sys.path.insert(0, str(HELPER))
    import helper
    folder = Path(folder)
    tools = make_tools(folder / 'tools')
    big = tools / 'ffmpeg'
    with open(big, 'wb') as f:
        for _ in range(300):
            f.write(os.urandom(2 ** 20))
    h = hashlib.sha256()
    with open(big, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    digest = h.hexdigest()
    (folder / 'm.json').write_text(json.dumps({'product': 'BraveMusicDownloader', 'version': '2.3.0', 'components': [
        {'name': 'ffmpeg', 'owned': True, 'sha256': 'archive-hash-not-the-file-hash', 'file_sha256': None}]}))
    helper.configure(output=folder / 'music', job_cache=folder / 'c', mb_cache=folder / 'mb', tools_dir=tools, manifest=folder / 'm.json')
    before = child_usage()['self_maxrss_mb']
    t = now_ms()
    rep = helper.diagnostics(deep=True)
    ms = now_ms() - t
    after = child_usage()['self_maxrss_mb']
    return {'file_mb': 300, 'diagnostics_deep_ms': round(ms, 1), 'rss_before_mb': round(before, 1), 'rss_after_mb': round(after, 1),
            'rss_spike_mb': round(after - before, 1), 'hash_matches_install': rep['tools']['ffmpeg'].get('hash_matches_install')}


def sec_misc(quick):
    sys.path.insert(0, str(HELPER))
    res = {}
    base = tmpdir()
    tools = make_tools(base / 'tools')
    import helper
    helper.configure(output=base / 'music', job_cache=base / 'cache', mb_cache=base / 'mb', tools_dir=tools, manifest=base / 'm.json')
    helper.open_store(base / 'q.sqlite3')
    os.environ['BENCH_FIXTURE'] = '/dev/null'
    spawns = {'n': 0}
    import subprocess as sp
    real = sp.Popen

    class CP(real):
        def __init__(self, *a, **k):
            spawns['n'] += 1
            super().__init__(*a, **k)
    sp.Popen = CP
    t = [ _time(helper.diagnostics) for _ in range(5)]
    sp.Popen = real
    res['diagnostics_ms'] = stats(t)
    res['diagnostics_processes_spawned_per_call'] = spawns['n'] / 5
    res['diagnostics_cached_for_s'] = 30
    d = tmpdir()
    run = sh([sys.executable, __file__, '--child', 'deephash', str(d)], timeout=600)
    shutil.rmtree(d, ignore_errors=True)
    res['diagnostics_deep_hash'] = json.loads(run.stdout.strip().splitlines()[-1]) if run.returncode == 0 else {'error': run.stderr[-300:]}
    # sweep_cache scaling (the 5,000-row history cap is lifted here only so 10k job caches can exist)
    import store as _store_mod
    _store_mod.MAX_HISTORY = 10 ** 9
    store = helper.STORE
    sweep = {}
    for n in ((1000,) if quick else (1000, 10000)):
        store.clear_finished()
        helper.JOB_CACHE.mkdir(parents=True, exist_ok=True)
        ids = []
        for i in range(n):
            ident = store.enqueue([{'id': 's%010d' % i, 'title': 't'}], True, True, find_existing=None)['added'][0]
            ids.append(ident)
            (helper.JOB_CACHE / ident).mkdir()
        keep = _time(helper.sweep_cache)
        for ident in ids:
            store.update(ident, state='error')
        store.conn.execute("UPDATE jobs SET state='done'")
        store.conn.commit()
        remove = _time(helper.sweep_cache)
        sweep[str(n)] = {'scan_keep_all_ms': round(keep, 1), 'scan_and_remove_all_ms': round(remove, 1)}
    res['sweep_cache'] = sweep
    # payload sizes + static UI facts
    db = base / 'p.sqlite3'
    make_db(db, 2000)
    import store as store_mod
    s = store_mod.Store(db)
    rows, _ = s.page(50)
    page_bytes = len(json.dumps({'ok': True, 'data': {'jobs': rows, 'counts': s.counts(), 'next_cursor': 'x'}}).encode())
    res['jobs50_json_bytes'] = page_bytes
    ext = ROOT / 'app' / 'extension'
    ui = {}
    for name in ('dashboard.js', 'popup.js', 'background.js'):
        text = (ext / name).read_text(encoding='utf-8')
        ui[name] = {'timers': re.findall(r'(set(?:Interval|Timeout))\s*\([^;]*?,\s*(\d+)\s*\)', text) or re.findall(r'(set(?:Interval|Timeout))\([^)]*?(\d{3,6})\)', text)}
    dash = (ext / 'dashboard.js').read_text(encoding='utf-8')
    m = re.search(r'function fill\([^)]*\)\s*\{(.*?)\n\}', dash, re.S)
    ui['dashboard_fill_createElement_calls_per_job'] = len(re.findall(r'createElement', m.group(1))) if m else None
    ui['dashboard_fill_replaces_all_nodes_each_poll'] = bool(m and 'replaceChildren' in m.group(1))
    res['ui_static'] = ui
    if shutil.which('node'):
        (base / 'page.json').write_text(json.dumps({'ok': True, 'data': {'jobs': rows, 'counts': {}, 'next_cursor': None}}))
        js = ("const fs=require('fs');const t=fs.readFileSync(process.argv[1],'utf8');let s=process.hrtime.bigint();"
              "for(let i=0;i<2000;i++)JSON.parse(t);console.log(Number(process.hrtime.bigint()-s)/2000/1e6);")
        out = sh(['node', '-e', js, str(base / 'page.json')]).stdout.strip()
        res['node_json_parse_50_jobs_ms'] = round(float(out), 4) if out else None
    s.close()
    shutil.rmtree(base, ignore_errors=True)
    return res


def sec_installer(quick):
    out = tmpdir()
    sh([sys.executable, str(ROOT / 'scripts' / 'build_release.py'), '--out', str(out), '--skip-exe'], check=True)
    sizes = {p.name: p.stat().st_size for p in out.iterdir()}
    import zipfile
    with zipfile.ZipFile(out / 'Application.zip') as z:
        infos = z.infolist()
    res = {'bytes': sizes, 'payload_files': len(infos), 'payload_uncompressed_bytes': sum(i.file_size for i in infos),
           'note': 'Tool download sizes and install time need real measurement on Windows (bench/Measure-Windows.ps1).'}
    shutil.rmtree(out, ignore_errors=True)
    return res


SECTIONS = {'env': sec_env, 'host': sec_host, 'nm': sec_nm, 'reconnect': sec_reconnect, 'store': sec_store,
            'library': sec_library, 'pipeline': sec_pipeline, 'cancel': sec_cancel, 'misc': sec_misc, 'installer': sec_installer}


def main():
    args = sys.argv[1:]
    if args and args[0] == '--child':
        kind = args[1]
        if kind == 'library':
            print(json.dumps(child_library(int(args[2]), args[3])))
        elif kind == 'pipeline':
            print(json.dumps(child_pipeline(args[2], args[3], args[4])))
        elif kind == 'cancel':
            print(json.dumps(child_cancel(args[2], args[3], args[4])))
        elif kind == 'deephash':
            print(json.dumps(child_deephash(args[2])))
        return 0
    quick = '--quick' in args
    out = next((a.split('=', 1)[1] for a in args if a.startswith('--out=')), str(Path(__file__).resolve().parent / 'results' / 'baseline.json'))
    names = [a for a in args if not a.startswith('--')] or list(SECTIONS)
    results = json.loads(Path(out).read_text()) if Path(out).exists() else {}
    for name in names:
        t = time.time()
        print(f'== {name} ...', flush=True)
        value = SECTIONS[name](quick)
        if os.environ.get('BENCH_MERGE') and isinstance(results.get(name), dict) and isinstance(value, dict):
            results[name].update(value)
        else:
            results[name] = value
        results.setdefault('_timing_s', {})[name] = round(time.time() - t, 1)
        Path(out).write_text(json.dumps(results, indent=2))
        print(f'   done in {time.time() - t:.1f}s', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
