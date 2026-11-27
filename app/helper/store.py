"""SQLite job store: WAL + synchronous=FULL, indexed, keyset-paginated.

Schema history
  user_version 0  v2.0.x database (jobs table only)  -> migrated in place after a backup
  user_version 1  v2.3.0: rank/ord paging columns, composite indexes, library index
"""
import base64
import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

SCHEMA_VERSION = 1
STATES = {'queued', 'running', 'done', 'error', 'canceled'}
FINAL_STATES = {'done', 'error', 'canceled'}
JOB_ID = re.compile(r'^[0-9a-f]{32}$')
MAX_HISTORY = 5000


def _rank(state):
    return 0 if state == 'running' else 1 if state == 'queued' else 2


def _order(state, created):
    # One ascending sort: running/queued oldest-first, finished newest-first.
    return created if _rank(state) < 2 else -created


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        self.conn.execute('PRAGMA busy_timeout=10000')
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=FULL')
        self.backup_path = None
        self._migrate()
        self.recovered = self.recover_interrupted()

    # -- schema ---------------------------------------------------------
    def _migrate(self):
        c = self.conn
        version = c.execute('PRAGMA user_version').fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError('Queue database was created by a newer Brave Music Downloader')
        if version == SCHEMA_VERSION:
            return
        has_jobs = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone()
        if has_jobs:
            backup = self.path.with_name(self.path.name + '.pre-2.3.bak')
            if not backup.exists():
                target = sqlite3.connect(str(backup))
                try:
                    c.backup(target)
                finally:
                    target.close()
                self.backup_path = backup
        c.execute('BEGIN IMMEDIATE')
        try:
            c.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, video_id TEXT NOT NULL, state TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, payload TEXT NOT NULL)''')
            columns = {row[1] for row in c.execute('PRAGMA table_info(jobs)')}
            if 'rank' not in columns:
                c.execute('ALTER TABLE jobs ADD COLUMN rank INTEGER NOT NULL DEFAULT 2')
            if 'ord' not in columns:
                c.execute('ALTER TABLE jobs ADD COLUMN ord REAL NOT NULL DEFAULT 0')
            c.execute("""UPDATE jobs SET
                rank = CASE state WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,
                ord  = CASE WHEN state IN ('running','queued') THEN created ELSE -created END""")
            c.execute('CREATE INDEX IF NOT EXISTS jobs_page ON jobs(rank, ord, id)')
            c.execute('CREATE INDEX IF NOT EXISTS jobs_state_ord ON jobs(state, ord, id)')
            c.execute('CREATE INDEX IF NOT EXISTS jobs_video ON jobs(video_id)')
            c.execute('''CREATE TABLE IF NOT EXISTS library (
                video_id TEXT PRIMARY KEY, path TEXT NOT NULL, size INTEGER NOT NULL, added REAL NOT NULL)''')
            c.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
            c.execute('COMMIT')
        except BaseException:
            c.execute('ROLLBACK')
            raise

    def recover_interrupted(self):
        """Jobs left 'running' by a crash or power loss go back to the queue."""
        recovered = []
        with self.lock:
            rows = self.conn.execute("SELECT id, payload FROM jobs WHERE state='running'").fetchall()
            for ident, packed in rows:
                job = json.loads(packed)
                job.update(state='queued', stage='queued', percent=0,
                           message='Resumed after helper restart')
                self._write_locked(job)
                recovered.append(ident)
            self.conn.commit()
        return recovered

    def info(self):
        with self.lock:
            return {
                'schema': self.conn.execute('PRAGMA user_version').fetchone()[0],
                'journal_mode': self.conn.execute('PRAGMA journal_mode').fetchone()[0],
                'synchronous': self.conn.execute('PRAGMA synchronous').fetchone()[0],
                'integrity': self.conn.execute('PRAGMA quick_check').fetchone()[0],
                'library_entries': self.conn.execute('SELECT COUNT(*) FROM library').fetchone()[0],
            }

    # -- jobs -----------------------------------------------------------
    def _write_locked(self, job):
        job['updated'] = time.time()
        self.conn.execute(
            'UPDATE jobs SET state=?, updated=?, payload=?, rank=?, ord=? WHERE id=?',
            (job['state'], job['updated'], json.dumps(job, ensure_ascii=False),
             _rank(job['state']), _order(job['state'], job['created']), job['id']))

    def get(self, ident):
        with self.lock:
            row = self.conn.execute('SELECT payload FROM jobs WHERE id=?', (ident,)).fetchone()
            return json.loads(row[0]) if row else None

    def counts(self):
        with self.lock:
            return dict(self.conn.execute('SELECT state, COUNT(*) FROM jobs GROUP BY state').fetchall())

    @staticmethod
    def _encode_cursor(rank, order, ident):
        raw = json.dumps([rank, order, ident], separators=(',', ':')).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip('=')

    @staticmethod
    def _decode_cursor(cursor):
        try:
            if not isinstance(cursor, str) or len(cursor) > 200:
                raise ValueError
            raw = base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4))
            rank, order, ident = json.loads(raw)
            if rank not in (0, 1, 2) or not isinstance(order, (int, float)) \
                    or isinstance(order, bool) or not JOB_ID.fullmatch(str(ident)):
                raise ValueError
            return rank, float(order), ident
        except (ValueError, TypeError):
            raise ValueError('Invalid page cursor') from None

    def page(self, limit=50, cursor=None, state=None):
        """Return (jobs, next_cursor). Stable keyset paging, newest finished first."""
        if state is not None and state != 'active' and state not in STATES:
            raise ValueError('Unknown job state filter')
        limit = max(1, min(int(limit), 200))
        where, args = [], []
        single = state not in (None, 'active')
        if state == 'active':
            where.append("state IN ('queued','running')")
        elif state:
            where.append('state=?')
            args.append(state)
        if cursor:
            rank, order, ident = self._decode_cursor(cursor)
            if single:
                where.append('(ord, id) > (?, ?)')
                args += [order, ident]
            else:
                where.append('(rank, ord, id) > (?, ?, ?)')
                args += [rank, order, ident]
        sql = 'SELECT rank, ord, id, payload FROM jobs'
        if where:
            sql += ' WHERE ' + ' AND '.join(where)
        sql += ' ORDER BY ' + ('ord, id' if single else 'rank, ord, id') + ' LIMIT ?'
        with self.lock:
            rows = self.conn.execute(sql, args + [limit + 1]).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = self._encode_cursor(*rows[-1][:3]) if more and rows else None
        return [json.loads(r[3]) for r in rows], next_cursor

    def next_queued(self):
        with self.lock:
            row = self.conn.execute(
                "SELECT payload FROM jobs WHERE state='queued' ORDER BY ord, id LIMIT 1").fetchone()
            if not row:
                return None
            job = json.loads(row[0])
            job.update(state='running', stage='preparing', message='Starting job…', percent=0)
            self._write_locked(job)
            self.conn.commit()
            return job

    def update(self, ident, **values):
        with self.lock:
            job = self.get(ident)
            if not job or job['state'] in ('done', 'canceled'):
                return job
            job.update(values)
            self._write_locked(job)
            self.conn.commit()
            return job

    def enqueue(self, items, metadata, normalize, musicbrainz=True, artwork=True,
                organize=True, trim_start=False, find_existing=None, format='wav'):
        result = {'added': [], 'duplicate': [], 'existing': []}
        with self.lock:
            count = self.conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
            if count + len(items) > MAX_HISTORY:
                raise ValueError('Queue history limit reached; clear finished jobs first')
            seen = set()
            for item in items:
                video_id = item['id']
                if video_id in seen:
                    continue
                seen.add(video_id)
                if find_existing and find_existing(video_id):
                    result['existing'].append(video_id)
                    continue
                active = self.conn.execute(
                    "SELECT 1 FROM jobs WHERE video_id=? AND state IN ('queued','running') LIMIT 1",
                    (video_id,)).fetchone()
                if active:
                    result['duplicate'].append(video_id)
                    continue
                ident, created = uuid.uuid4().hex, time.time()
                job = dict(id=ident, video_id=video_id,
                           url='https://www.youtube.com/watch?v=' + video_id,
                           title=item.get('title') or 'YouTube track', state='queued', stage='queued',
                           percent=0, message='Waiting in queue…', attempts=0, created=created,
                           updated=created, metadata=metadata, normalize=normalize,
                           musicbrainz=musicbrainz, artwork=artwork, organize=organize,
                           trim_start=trim_start, format=format,
                           metadata_match='pending', artwork_source='',
                           album='', artist='', filename='', folder='', size_bytes=0)
                self.conn.execute(
                    'INSERT INTO jobs (id, video_id, state, created, updated, payload, rank, ord) '
                    'VALUES (?,?,?,?,?,?,?,?)',
                    (ident, video_id, 'queued', created, created, json.dumps(job, ensure_ascii=False),
                     1, created))
                result['added'].append(ident)
            self.conn.commit()
        return result

    def cancel(self, ident):
        with self.lock:
            job = self.get(ident)
            if not job:
                raise KeyError('Unknown job')
            if job['state'] in FINAL_STATES:
                raise ValueError('This job is already finished')
            job.update(state='canceled', stage='canceled', percent=None, message='Canceled by user')
            self._write_locked(job)
            self.conn.commit()
            return job

    def retry(self, ident, find_existing=None):
        with self.lock:
            job = self.get(ident)
            if not job:
                raise KeyError('Unknown job')
            if job['state'] not in {'error', 'canceled'}:
                raise ValueError('Only failed or canceled jobs can be retried')
            if find_existing and find_existing(job['video_id']):
                raise ValueError('Output already exists; skip duplicate')
            job.update(state='queued', stage='queued', percent=0, attempts=0,
                       message='Queued for retry…')
            self._write_locked(job)
            self.conn.commit()
            return job

    def clear_finished(self):
        with self.lock:
            cur = self.conn.execute("DELETE FROM jobs WHERE state IN ('done','error','canceled')")
            self.conn.commit()
            return cur.rowcount

    def states(self, ids):
        """Map job id -> (state, updated) for the ids that still exist."""
        found = {}
        with self.lock:
            for ident in ids:
                row = self.conn.execute('SELECT state, updated FROM jobs WHERE id=?', (ident,)).fetchone()
                if row:
                    found[ident] = (row[0], row[1])
        return found

    # -- library index (video id -> saved WAV) ------------------------------
    def library_get(self, video_id):
        with self.lock:
            row = self.conn.execute('SELECT path FROM library WHERE video_id=?', (video_id,)).fetchone()
            return row[0] if row else None

    def library_put(self, video_id, path, size):
        with self.lock:
            self.conn.execute('INSERT OR REPLACE INTO library VALUES (?,?,?,?)',
                              (video_id, str(path), int(size), time.time()))
            self.conn.commit()

    def library_upsert_many(self, rows):
        """Index many (video_id, path, size, added) rows in ONE transaction; returns rows inserted or changed.

        Unchanged rows are left untouched (no write), so rescanning an unchanged library writes nothing.
        """
        with self.lock:
            before = self.conn.total_changes
            self.conn.executemany(
                'INSERT INTO library (video_id, path, size, added) VALUES (?,?,?,?) '
                'ON CONFLICT(video_id) DO UPDATE SET path=excluded.path, size=excluded.size '
                'WHERE library.path != excluded.path OR library.size != excluded.size', rows)
            self.conn.commit()
            return self.conn.total_changes - before

    def library_page(self, after='', limit=2000):
        """Keyset page over the index (releases the lock between pages)."""
        with self.lock:
            return self.conn.execute('SELECT video_id, path FROM library WHERE video_id > ? ORDER BY video_id LIMIT ?',
                                     (after, limit)).fetchall()

    def library_forget_many(self, video_ids):
        with self.lock:
            self.conn.executemany('DELETE FROM library WHERE video_id=?', [(v,) for v in video_ids])
            self.conn.commit()

    def library_forget(self, video_id):
        with self.lock:
            self.conn.execute('DELETE FROM library WHERE video_id=?', (video_id,))
            self.conn.commit()

    def close(self):
        with self.lock:
            self.conn.close()
