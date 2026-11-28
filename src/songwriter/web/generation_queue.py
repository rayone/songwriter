"""Persistent, single-worker song generation queue.

The queue owns stable song IDs before starting work.  On restart it checks the
song store before retrying an interrupted attempt, avoiding duplicate songs when
saving succeeded but the queue update did not.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from songwriter.ai.generation import SongGeneration


class GenerationQueue:
    def __init__(self, path: Path, cowriter: Any):
        self.path = path
        self.cowriter = cowriter
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS items (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, position INTEGER NOT NULL,
                    title TEXT NOT NULL, status TEXT NOT NULL, song_id TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0, attempt_revision INTEGER NOT NULL DEFAULT 0,
                    document TEXT NOT NULL, error TEXT, incomplete_sections TEXT NOT NULL DEFAULT '[]',
                    FOREIGN KEY(job_id) REFERENCES jobs(id)
                );
            ''')

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.stopping.clear()
            with self._connect() as db:
                # Interrupted attempts are reconciled by the worker before restarting.
                db.execute("UPDATE jobs SET status='queued', updated_at=CURRENT_TIMESTAMP WHERE status='running'")
            self.thread = threading.Thread(target=self._work, daemon=True, name='song-generation-queue')
            self.thread.start()
            self.wake.set()

    def stop(self):
        self.stopping.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=1)

    def create(self, songs: list[SongGeneration]) -> dict:
        if not songs or len(songs) > 200:
            raise ValueError('Provide between 1 and 200 songs.')
        job_id = uuid.uuid4().hex
        with self.lock, self._connect() as db:
            db.execute("INSERT INTO jobs(id,status) VALUES (?, 'queued')", (job_id,))
            for n, song in enumerate(songs):
                if song.song_id:
                    raise ValueError('Queue new songs without a song_id; use retry for an existing queue item.')
                song.song_id = 'song_' + uuid.uuid4().hex
                song.take_group = ''
                song.take_number = 0
                db.execute('INSERT INTO items(id,job_id,position,title,status,song_id,document) VALUES (?,?,?,?,?,?,?)',
                           (uuid.uuid4().hex, job_id, n, song.title, 'queued', song.song_id, song.model_dump_json()))
        self.wake.set()
        return self.get(job_id)

    def _job(self, db, row):
        job = dict(row)
        job['items'] = []
        for item in db.execute('SELECT id,title,status,song_id,revision,error,incomplete_sections FROM items WHERE job_id=? ORDER BY position', (job['id'],)):
            obj = dict(item)
            obj['incomplete_sections'] = json.loads(obj['incomplete_sections'])
            job['items'].append(obj)
        return job

    def list(self):
        with self.lock, self._connect() as db:
            return [self._job(db, row) for row in db.execute('SELECT * FROM jobs ORDER BY rowid DESC').fetchall()]

    def get(self, job_id):
        with self.lock, self._connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not row:
                raise KeyError('Queue job not found')
            return self._job(db, row)

    def resume(self, job_id, model=None):
        with self.lock, self._connect() as db:
            job = db.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not job:
                raise KeyError('Queue job not found')
            if job['status'] != 'paused':
                raise ValueError('Only a paused job can be resumed.')
            if model:
                rows = db.execute("SELECT id,document FROM items WHERE job_id=? AND status='queued'", (job_id,)).fetchall()
                for row in rows:
                    document = json.loads(row['document'])
                    document['model'] = model
                    db.execute('UPDATE items SET document=? WHERE id=?', (json.dumps(document), row['id']))
            db.execute("UPDATE jobs SET status='queued',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
        self.wake.set()
        return self.get(job_id)

    def retry(self, job_id, item_id):
        with self.lock, self._connect() as db:
            item = db.execute('SELECT * FROM items WHERE id=? AND job_id=?', (item_id, job_id)).fetchone()
            if not item:
                raise KeyError('Queue item not found')
            if item['status'] not in ('incomplete', 'failed'):
                raise ValueError('Only incomplete or failed items can be retried.')
            current = self.cowriter.load_generation(item['song_id'])
            actual = current.revision if current else 0
            if actual != item['revision']:
                raise ValueError('The saved song changed since this attempt. Open it in Saved Songs instead.')
            db.execute("UPDATE items SET status='queued', error=NULL, attempt_revision=? WHERE id=?", (actual, item_id))
            db.execute("UPDATE jobs SET status='queued',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='completed'", (job_id,))
        self.wake.set()
        return self.get(job_id)

    def _work(self):
        while not self.stopping.is_set():
            try:
                if not self._step():
                    self.wake.wait(2)
                    self.wake.clear()
            except Exception:
                # Keep the worker alive after a transient storage error.
                import logging
                logging.exception('Generation queue worker failed')
                self.stopping.wait(2)

    def _step(self):
        with self.lock, self._connect() as db:
            row = db.execute("SELECT i.* FROM items i JOIN jobs j ON j.id=i.job_id WHERE j.status IN ('queued','running') AND i.status IN ('queued','running') ORDER BY j.rowid,i.position LIMIT 1").fetchone()
            if not row:
                db.execute("UPDATE jobs SET status='completed', updated_at=CURRENT_TIMESTAMP WHERE status IN ('queued','running') AND NOT EXISTS (SELECT 1 FROM items WHERE job_id=jobs.id AND status IN ('queued','running'))")
                return False
            item = dict(row)
            # A crash can occur after the song was saved but before the queue update.
            if item['status'] == 'running':
                current = self.cowriter.load_generation(item['song_id'])
                if current and current.revision > item['attempt_revision']:
                    self._record_song(db, item, current)
                    return True
            else:
                current = self.cowriter.load_generation(item['song_id'])
                if current and current.revision != item['revision']:
                    db.execute("UPDATE items SET status='failed',error=? WHERE id=?", ('Saved song changed outside the queue; retry stopped to protect it.', item['id']))
                    return True
                db.execute("UPDATE items SET status='running',attempt_revision=revision WHERE id=?", (item['id'],))
                item['attempt_revision'] = item['revision']
            db.execute("UPDATE jobs SET status='running', updated_at=CURRENT_TIMESTAMP WHERE id=?", (item['job_id'],))
        try:
            song = SongGeneration.model_validate_json(item['document'])
            song.song_id = item['song_id']
            song.revision = item['attempt_revision']
            complete = None
            for event in self.cowriter.stream_song_interactive(song):
                if event.get('type') == 'error':
                    raise RuntimeError(event.get('message', 'Generation failed'))
                if event.get('type') == 'complete':
                    complete = SongGeneration.model_validate(event['song'])
            if complete is None:
                raise RuntimeError('Generation ended without a completed song.')
            with self.lock, self._connect() as db:
                self._record_song(db, item, complete)
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            with self.lock, self._connect() as db:
                # Reconcile an exception after save before exposing a retry.
                current = self.cowriter.load_generation(item['song_id'])
                if current and current.revision > item['attempt_revision']:
                    self._record_song(db, item, current)
                elif '429' in message or 'quota' in message.lower() or 'rate limit' in message.lower():
                    db.execute("UPDATE items SET status='queued',error=? WHERE id=?", (message, item['id']))
                    db.execute("UPDATE jobs SET status='paused',error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (message, item['job_id']))
                else:
                    db.execute("UPDATE items SET status='failed',error=? WHERE id=?", (message, item['id']))
        return True

    @staticmethod
    def _record_song(db, item, song):
        status = 'incomplete' if song.status == 'incomplete' else 'completed'
        db.execute('UPDATE items SET status=?,revision=?,error=NULL,incomplete_sections=? WHERE id=?',
                   (status, song.revision, json.dumps(song.incomplete_sections), item['id']))
