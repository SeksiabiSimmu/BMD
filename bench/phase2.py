#!/usr/bin/env python3
"""Targeted before/after micro-benchmarks for the Phase 2 changes.

  python bench/phase2.py progress library reconnect deephash --out=bench/results/phase2-before.json
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import run as R
from common import FAKE_YTDLP, HELPER, now_ms, stats, tmpdir


def child_progress(lines, sleep_ms, folder):
    sys.path.insert(0, str(HELPER))
    import helper
    folder = Path(folder)
    tools = R.make_tools(folder / 'tools')
    os.environ.update(BENCH_FIXTURE=str(R.CACHE / 'music-180s.m4a'), BENCH_PROGRESS_LINES=str(lines),
                      BENCH_PROGRESS_SLEEP_MS=str(sleep_ms))
    helper.configure(output=folder / 'music', job_cache=folder / 'cache' / 'jobs', mb_cache=folder / 'mb', tools_dir=tools)
    helper.MIN_FREE_BYTES = 1
    store = helper.open_store(folder / 'q.sqlite3')
    ident = store.enqueue([{'id': 'p0000000000', 'title': 'T'}], True, False, musicbrainz=False, find_existing=None)['added'][0]
    store.next_queued()
    work = folder / 'work'
    work.mkdir()
    n, spent = {'u': 0}, {'ms': 0.0}
    real = store.update

    def counting(*a, **k):
        t = now_ms()
        try:
            return real(*a, **k)
        finally:
            n['u'] += 1
            spent['ms'] += now_ms() - t
    store.update = counting
    latencies, stop = [], threading.Event()

    def reader():                      # the UI polling the queue while the download runs
        while not stop.is_set():
            t = now_ms()
            store.page(50)
            latencies.append(now_ms() - t)
            time.sleep(.005)
    threading.Thread(target=reader, daemon=True).start()
    t0 = now_ms()
    helper.streamed_download(ident, 'https://www.youtube.com/watch?v=p0000000000', work, artwork=False)
    wall = now_ms() - t0
    stop.set()
    final = store.get(ident)
    return {'lines': lines, 'sleep_ms': sleep_ms, 'wall_ms': round(wall), 'store_updates': n['u'],
            'update_total_ms': round(spent['ms'], 1), 'ui_page_latency_ms': stats(latencies),
            'final_percent': final['percent'], 'final_message': final['message']}


def sec_progress(quick):
    out = {}
    for lines, sleep in ((50, 100), (300, 10)):
        d = tmpdir()
        run = R.sh([sys.executable, __file__, '--child', 'progress', str(lines), str(sleep), str(d)], timeout=300)
        shutil.rmtree(d, ignore_errors=True)
        out[f'{lines}_lines_every_{sleep}ms'] = json.loads(run.stdout.strip().splitlines()[-1]) if run.returncode == 0 else {'error': run.stderr[-500:]}
    return out


def sec_library(quick):
    out = {}
    for n in (10000, 100000):
        d = tmpdir()
        run = R.sh([sys.executable, R.__file__, '--child', 'library', str(n), str(d)], timeout=1800)
        shutil.rmtree(d, ignore_errors=True)
        v = json.loads(run.stdout.strip().splitlines()[-1])
        out[str(n)] = {k: v[k] for k in ('first_reconcile_ms', 'first_reconcile_commits', 'second_reconcile_ms', 'peak_rss_mb', 'rss_before_mb')}
    return out


def sec_reconnect(quick):
    r = R.sec_reconnect(False)
    return {m: r[m]['until_new_helper_answers_ms'] for m in ('graceful_exit', 'killed')}


def sec_deephash(quick):
    d = tmpdir()
    run = R.sh([sys.executable, R.__file__, '--child', 'deephash', str(d)], timeout=600)
    shutil.rmtree(d, ignore_errors=True)
    return json.loads(run.stdout.strip().splitlines()[-1])


SECTIONS = {'progress': sec_progress, 'library': sec_library, 'reconnect': sec_reconnect, 'deephash': sec_deephash}

if __name__ == '__main__':
    args = sys.argv[1:]
    if args and args[0] == '--child':
        print(json.dumps(child_progress(int(args[2]), int(args[3]), args[4])))
        sys.exit(0)
    out = next((a.split('=', 1)[1] for a in args if a.startswith('--out=')), 'bench/results/phase2.json')
    names = [a for a in args if not a.startswith('--')] or list(SECTIONS)
    res = json.loads(Path(out).read_text()) if Path(out).exists() else {}
    for name in names:
        print('==', name, flush=True)
        res[name] = SECTIONS[name](False)
        Path(out).write_text(json.dumps(res, indent=2))
