"""Single DuckDB gateway: one object per file, per process.

DuckDB refuses a second connection to a file already open in this process with a
different configuration (read-only vs read-write), and a read-write connection locks
the file against every other process. Opening files ad hoc from every module is what
produced lock and "different configuration" errors. Every DuckDB access in ``src/``
goes through ``Database.get``:

* each file is opened in exactly one mode per process;
* read-only files keep one shared connection, with per-thread cursors for reads;
* read-write files open a short-lived connection per read or transaction, so the
  write lock is held only for milliseconds and other processes (dev server, tests,
  scripts) can use the same file;
* writes run under one lock inside a transaction, rolled back on error;
* read-only files that get *replaced* on disk (the rhyme dictionary rebuild writes a
  new file rather than updating in place) are reopened transparently.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import duckdb

from songwriter.config import CORPUS_DB, WORKBENCH_DB


def _stamp(path: Path) -> tuple[int, int, float] | None:
    """Identity of the file on disk: (inode, size, mtime)."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime)


def duckdb_config() -> dict[str, str]:
    from songwriter.config import DUCKDB_THREADS, DUCKDB_MEMORY_LIMIT
    cfg = {}
    if DUCKDB_THREADS:
        cfg["threads"] = str(DUCKDB_THREADS)
    if DUCKDB_MEMORY_LIMIT:
        cfg["memory_limit"] = str(DUCKDB_MEMORY_LIMIT)
    return cfg


class Database:
    """Process-wide gateway to one DuckDB file."""

    _registry: dict[Path, Database] = {}
    _registry_lock = threading.Lock()

    def __init__(self, path: Path, read_only: bool) -> None:
        self.path = path
        self.read_only = read_only
        self._write_lock = threading.Lock()
        self._reopen_lock = threading.Lock()
        self._local = threading.local()
        self._generation = 0
        # Read-only files keep one shared connection: any number of processes may hold
        # a read-only lock at once. A read-write file is opened per operation instead,
        # because its lock excludes every other process (a dev server, tests, scripts)
        # for as long as the connection stays open.
        self._root = self._open() if read_only else None

    # ------------------------------------------------------------ lifecycle

    @classmethod
    def get(cls, path: Path | str, read_only: bool = True) -> Database:
        """The shared gateway for ``path``.

        A file is opened in exactly one mode per process. Asking for write access to a
        file already opened read-only (or the reverse) is a programming error and raises
        instead of silently opening a conflicting second connection.
        """
        key = Path(path).resolve()
        with cls._registry_lock:
            db = cls._registry.get(key)
            if db is None:
                db = cls(key, read_only)
                cls._registry[key] = db
            elif db.read_only != read_only:
                raise RuntimeError(
                    f"{key.name} is already open {'read-only' if db.read_only else 'read-write'} "
                    f"in this process; cannot reopen it {'read-only' if read_only else 'read-write'}."
                )
            return db

    @classmethod
    def close_all(cls) -> None:
        with cls._registry_lock:
            for db in cls._registry.values():
                db._close_root()
            cls._registry.clear()


    def _open(self) -> duckdb.DuckDBPyConnection:
        if self.read_only and not self.path.exists():
            raise FileNotFoundError(self.path)
        self._file_stamp = _stamp(self.path)
        return self._connect()

    def _connect(self, wait_s: float = 5.0) -> duckdb.DuckDBPyConnection:
        """Connect, waiting briefly if another process holds the file's write lock."""
        deadline = time.monotonic() + wait_s
        while True:
            try:
                cfg = duckdb_config()
                if cfg:
                    return duckdb.connect(str(self.path), read_only=self.read_only, config=cfg)
                return duckdb.connect(str(self.path), read_only=self.read_only)
            except duckdb.IOException as exc:
                if "lock" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def _close_root(self) -> None:
        if self._root is None:
            return
        try:
            self._root.close()
        except Exception:
            pass

    def _refresh_if_replaced(self) -> None:
        """Reopen a read-only file that was replaced on disk by a rebuild."""
        if not self.read_only:
            return
        current = _stamp(self.path)
        if current is None or current == self._file_stamp:
            return
        with self._reopen_lock:
            if _stamp(self.path) == self._file_stamp:
                return
            self._close_root()
            self._root = self._open()
            self._generation += 1

    # --------------------------------------------------------------- access

    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        """This thread's cursor on a read-only database. Do not close it."""
        if not self.read_only:
            raise RuntimeError(f"{self.path.name} is read-write; use read*/cursor()/transaction().")
        self._refresh_if_replaced()
        held = getattr(self._local, "cursor", None)
        if held is None or held[0] != self._generation:
            held = (self._generation, self._root.cursor())
            self._local.cursor = held
        return held[1]

    def cursor(self) -> duckdb.DuckDBPyConnection:
        """A connection the caller owns and must close.

        Read-only: a cursor on the shared connection. Read-write: a fresh connection;
        closing it releases the file lock for other processes.
        """
        if not self.read_only:
            return self._connect()
        self._refresh_if_replaced()
        return self._root.cursor()

    @contextmanager
    def _reader(self) -> Iterator[duckdb.DuckDBPyConnection]:
        if self.read_only:
            yield self.conn
            return
        con = self._connect()
        try:
            yield con
        finally:
            con.close()

    def read(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        with self._reader() as con:
            return con.execute(sql, params).fetchall()

    def read_one(self, sql: str, params: Sequence[Any] = ()) -> tuple | None:
        with self._reader() as con:
            return con.execute(sql, params).fetchone()

    def read_dicts(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._reader() as con:
            cur = con.execute(sql, params)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    @contextmanager
    def transaction(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """Serialized write transaction; commits on success, rolls back on error."""
        if self.read_only:
            raise RuntimeError(f"{self.path.name} is open read-only; writes are not allowed.")
        with self._write_lock:
            con = self._connect()
            try:
                con.execute("BEGIN TRANSACTION")
                try:
                    yield con
                except BaseException:
                    con.execute("ROLLBACK")
                    raise
                con.execute("COMMIT")
            finally:
                con.close()

    def write(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self.transaction() as cur:
            cur.execute(sql, params)


def corpus_db() -> Database:
    """Read-only corpus, templates, lexicon and rhyme dictionary."""
    return Database.get(CORPUS_DB, read_only=True)


def workbench_db() -> Database:
    """Read-write store for crafted songs and MusicStudio render records."""
    return Database.get(WORKBENCH_DB, read_only=False)
