"""Shared helpers for the Brave Music Downloader benchmark harness (does not touch app code)."""
import json
import os
import platform
import resource
import shutil
import sqlite3
import statistics
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'app' / 'helper'
STATES = ['done'] * 80 + ['error'] * 15 + ['canceled'] * 2 + ['queued'] * 2 + ['running'] * 1


def now_ms():
    return time.perf_counter() * 1000


def stats(samples):
    s = sorted(samples)
    n = len(s)
    pick = lambda q: s[min(n - 1, int(round(q * (n - 1))))]
    return {'n': n, 'min': round(s[0], 3), 'p50': round(pick(.5), 3), 'p95': round(pick(.95), 3),
            'max': round(s[-1], 3), 'mean': round(statistics.fmean(s), 3)}


def tmpdir(prefix='bmdbench-'):
    return Path(tempfile.mkdtemp(prefix=prefix, dir=os.environ.get('BENCH_TMP')))


def env_info():
    fs = []
    d = tmpdir('fsync-')
    f = open(d / 'f', 'wb')
    for _ in range(60):
        f.write(b'x' * 4096)
        f.flush()
        t = now_ms()
        os.fsync(f.fileno())
        fs.append(now_ms() - t)
    f.close()
    shutil.rmtree(d, ignore_errors=True)
    ff = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True).stdout.splitlines()[0]
    return {'platform': platform.platform(), 'cpus': os.cpu_count(), 'python': sys.version.split()[0],
            'sqlite': sqlite3.sqlite_version, 'ffmpeg': ff.split(' Copyright')[0],
            'ram_mb': round(os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') / 2 ** 20),
            'fsync_4k_ms': stats(fs)}


def host_env(base):
    base = Path(base)
    return dict(os.environ, BMD_INSTALL_ROOT=str(base / 'root'), BMD_DATA_DIR=str(base / 'data'),
                BMD_OUTPUT=str(base / 'music'), PYTHONUTF8='1')


def frame(obj):
    raw = json.dumps(obj).encode()
    return struct.pack('<I', len(raw)) + raw


def read_frame(stream):
    head = b''
    while len(head) < 4:
        chunk = stream.read(4 - len(head))
        if not chunk:
            raise EOFError('host closed output')
        head += chunk
    size = struct.unpack('<I', head)[0]
    body = b''
    while len(body) < size:
        chunk = stream.read(size - len(body))
        if not chunk:
            raise EOFError('host closed output')
        body += chunk
    return json.loads(body), size


def proc_stats(pid):
    """(cpu seconds, peak RSS MB) of a live process from /proc."""
    tick = os.sysconf('SC_CLK_TCK')
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    cpu = (int(fields[11]) + int(fields[12])) / tick
    peak = 0
    for line in Path(f'/proc/{pid}/status').read_text().splitlines():
        if line.startswith('VmHWM'):
            peak = int(line.split()[1]) / 1024
    return round(cpu, 3), round(peak, 1)


class Host:
    def __init__(self, host_dir, env):
        self.t0 = now_ms()
        self.proc = subprocess.Popen([sys.executable, str(Path(host_dir) / 'native_host.py')],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.seq = 0

    def request(self, method, params=None):
        self.seq += 1
        t = now_ms()
        self.proc.stdin.write(frame({'id': self.seq, 'method': method, 'params': params or {}}))
        self.proc.stdin.flush()
        reply, size = read_frame(self.proc.stdout)
        return now_ms() - t, reply, size

    def send(self, method, params=None):
        self.seq += 1
        self.proc.stdin.write(frame({'id': self.seq, 'method': method, 'params': params or {}}))

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        for s in (self.proc.stdout, self.proc.stderr):
            s.close()


def make_db(path, rows):
    """A queue database with `rows` realistic history rows, built through the app's own Store."""
    sys.path.insert(0, str(HELPER))
    import store as store_mod
    s = store_mod.Store(path)
    base = time.time() - rows * 60
    batch = []
    for i in range(rows):
        state = STATES[i % len(STATES)]
        ident = uuid.uuid4().hex
        created = base + i * 60
        job = dict(id=ident, video_id='v%010d' % i, url='https://www.youtube.com/watch?v=v%010d' % i,
                   title='Track number %d - An Artist Name (Official Audio)' % i, state=state, stage=state,
                   percent=100 if state == 'done' else 0, message='WAV saved successfully. Metadata: matched.',
                   attempts=1, created=created, updated=created, metadata=True, normalize=True, musicbrainz=True,
                   artwork=True, organize=True, trim_start=False, metadata_match='matched',
                   artwork_source='MusicBrainz Cover Art Archive', album='Some Album Title', artist='An Artist Name',
                   filename='Track number %d [v%010d].wav' % (i, i), folder='C:\\Users\\x\\Music\\Brave Music Downloader\\An Artist Name\\Some Album Title',
                   size_bytes=31457280)
        rank = 0 if state == 'running' else 1 if state == 'queued' else 2
        order = created if rank < 2 else -created
        batch.append((ident, job['video_id'], state, created, created, json.dumps(job, ensure_ascii=False), rank, order))
    with s.lock:
        s.conn.executemany('INSERT INTO jobs (id, video_id, state, created, updated, payload, rank, ord) VALUES (?,?,?,?,?,?,?,?)', batch)
        s.conn.commit()
    s.close()
    return Path(path).stat().st_size


def child_usage():
    c, me = resource.getrusage(resource.RUSAGE_CHILDREN), resource.getrusage(resource.RUSAGE_SELF)
    return {'child_cpu_s': c.ru_utime + c.ru_stime, 'self_cpu_s': me.ru_utime + me.ru_stime,
            'child_maxrss_mb': c.ru_maxrss / 1024, 'self_maxrss_mb': me.ru_maxrss / 1024,
            'block_out_mb': (c.ru_oublock + me.ru_oublock) * 512 / 2 ** 20}


def run_cmd(args, **kw):
    t = now_ms()
    run = subprocess.run(args, capture_output=True, text=True, **kw)
    return now_ms() - t, run


FAKE_YTDLP = r'''#!{python}
import json, os, shutil, sys, time
from pathlib import Path
args = sys.argv[1:]
template = Path(args[args.index('-o') + 1])
work = template.parent
mode = os.environ.get('BENCH_MODE', 'copy')
vid = args[-1].split('v=')[-1]
lines = int(os.environ.get('BENCH_PROGRESS_LINES', '60'))
fixture = Path(os.environ['BENCH_FIXTURE'])
if mode == 'sleep':
    for i in range(600):
        print('[download]  %d.0%% of 3.00MiB' % (i % 90), flush=True); time.sleep(.1)
    sys.exit(0)
pause = float(os.environ.get('BENCH_PROGRESS_SLEEP_MS', '0')) / 1000
for i in range(lines):
    print('[download]  %.1f%% of 3.00MiB at 2.0MiB/s ETA 00:01' % (100.0 * i / max(1, lines - 1)), flush=True)
    if pause: time.sleep(pause)
shutil.copyfile(fixture, work / ('source' + fixture.suffix))
info = json.loads(Path(str(fixture) + '.info.json').read_text())
info['id'] = vid
(work / 'source.info.json').write_text(json.dumps(info))
if '--write-thumbnail' in args:
    shutil.copyfile(str(fixture) + '.jpg', work / 'source.jpg')
'''
