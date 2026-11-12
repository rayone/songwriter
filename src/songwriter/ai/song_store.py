"""Persistence for crafted songs: one row per song, the whole SongGeneration as JSON.

Schema (workbench.duckdb):

  crafted_songs
    id, revision, status, created_at, updated_at   -- row lifecycle (stored once)
    audio_path, rating, notes                      -- post-generation user/render state
    generation JSON                                -- the complete SongGeneration
    title, genre, tempo_bpm, ... (VIRTUAL)         -- read-only projections of the JSON
                                                      for search, sort and the MusicStudio API

  song_revisions
    song_id, revision, created_at, generation JSON -- immutable snapshot per save

Virtual columns are computed from ``generation`` on read, so no fact is stored
twice: change the JSON and every projection follows.
"""

from __future__ import annotations

import json
import threading
import uuid
from typing import Any

from songwriter.ai.generation import SongGeneration
from songwriter.db import Database, workbench_db

SCHEMA = """
CREATE TABLE IF NOT EXISTS crafted_songs (
    id          VARCHAR PRIMARY KEY,
    revision    INTEGER NOT NULL DEFAULT 1,
    status      VARCHAR NOT NULL DEFAULT 'draft',
    created_at  TIMESTAMP DEFAULT current_timestamp,
    updated_at  TIMESTAMP DEFAULT current_timestamp,
    audio_path  VARCHAR,
    rating      INTEGER,
    notes       VARCHAR,
    generation  JSON NOT NULL,
    title        VARCHAR   GENERATED ALWAYS AS (generation->>'$.title') VIRTUAL,
    artist       VARCHAR   GENERATED ALWAYS AS (generation->>'$.artist') VIRTUAL,
    brief        VARCHAR   GENERATED ALWAYS AS (generation->>'$.brief') VIRTUAL,
    genre        VARCHAR   GENERATED ALWAYS AS (generation->>'$.genre') VIRTUAL,
    subgenre     VARCHAR   GENERATED ALWAYS AS (generation->>'$.subgenre') VIRTUAL,
    lyrics       VARCHAR   GENERATED ALWAYS AS (generation->>'$.lyrics') VIRTUAL,
    tempo_bpm    DOUBLE    GENERATED ALWAYS AS (CAST(generation->>'$.bpm' AS DOUBLE)) VIRTUAL,
    musical_key  VARCHAR   GENERATED ALWAYS AS (generation->>'$.key') VIRTUAL,
    scale        VARCHAR   GENERATED ALWAYS AS (generation->>'$.scale') VIRTUAL,
    vocal_style  VARCHAR   GENERATED ALWAYS AS (generation->>'$.vocal') VIRTUAL,
    model_name   VARCHAR   GENERATED ALWAYS AS (generation->>'$.model') VIRTUAL,
    abc_score    VARCHAR   GENERATED ALWAYS AS (generation->>'$.abc_score') VIRTUAL,
    style_prompt VARCHAR   GENERATED ALWAYS AS (generation->>'$.engine_prompts.songbench') VIRTUAL,
    engine_prompt VARCHAR  GENERATED ALWAYS AS (generation->>'$.engine_prompts.minimax') VIRTUAL,
    engine_target VARCHAR  GENERATED ALWAYS AS (generation->>'$.engine_target') VIRTUAL,
    chords       VARCHAR[] GENERATED ALWAYS AS (CAST(json_extract(generation, '$.chords') AS VARCHAR[])) VIRTUAL
);

CREATE TABLE IF NOT EXISTS song_revisions (
    song_id    VARCHAR NOT NULL,
    revision   INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT current_timestamp,
    generation JSON NOT NULL,
    status     VARCHAR,
    PRIMARY KEY (song_id, revision)
);
"""

_SONG_COLUMNS = """
    s.generation, s.id AS song_id, s.revision, s.status,
    s.created_at, s.updated_at, s.rating, s.notes,
    (SELECT count(*) FROM crafted_songs t
     WHERE COALESCE(NULLIF(t.generation->>'$.take_group', ''), t.id) =
           COALESCE(NULLIF(s.generation->>'$.take_group', ''), s.id)) AS take_count
"""

_REVISION_SOURCE = """
    (SELECT r.generation, r.song_id AS id, r.revision, r.status,
            c.created_at, r.created_at AS updated_at, c.rating, c.notes
     FROM song_revisions r JOIN crafted_songs c ON c.id = r.song_id) s
"""

# Sortable list columns; anything else falls back to updated_at.
_SORT_COLUMNS = {
    "updated_at": "updated_at", "created_at": "created_at", "title": "title",
    "bpm": "tempo_bpm", "genre": "genre", "status": "status",
}

_schema_lock = threading.Lock()
_schema_ready: set[str] = set()


def ensure_schema(db: Database | None = None) -> Database:
    """Create the song tables once per process."""
    db = db or workbench_db()
    key = str(db.path)
    if key not in _schema_ready:
        with _schema_lock:
            if key not in _schema_ready:
                with db.transaction() as cur:
                    cur.execute(SCHEMA)
                    cur.execute("ALTER TABLE song_revisions ADD COLUMN IF NOT EXISTS status VARCHAR")
                _schema_ready.add(key)
    return db


class SongStore:
    """Save / load / list / delete SongGeneration records."""

    def __init__(self, db: Database | None = None) -> None:
        self._db = db

    @property
    def db(self) -> Database:
        return ensure_schema(self._db)

    @staticmethod
    def _decode(row: dict[str, Any]) -> SongGeneration:
        document = json.loads(row.pop("generation"))
        return SongGeneration.from_record(document, **row)

    # ----------------------------------------------------------------- write

    def save(self, gen: SongGeneration) -> SongGeneration:
        """Save this instance and an immutable snapshot, without duplicating derived data."""
        if gen.status is None:
            raise ValueError("Choose a status before saving a historical revision with unknown status.")
        song_id = gen.song_id or f"song_{uuid.uuid4().hex[:10]}"
        if not gen.take_group:
            gen.take_group = song_id
        if gen.take_number <= 0:
            existing = self.db.read_one(
                "SELECT max(CAST(generation->>'$.take_number' AS INTEGER)) "
                "FROM crafted_songs WHERE generation->>'$.take_group' = ?",
                (gen.take_group,),
            )
            gen.take_number = int((existing[0] or 0)) + 1 if existing else 1
        document = gen.to_record()
        with self.db.transaction() as cur:
            row = cur.execute(
                "SELECT revision, status FROM crafted_songs WHERE id = ?", (song_id,),
            ).fetchone()
            latest = cur.execute(
                "SELECT generation FROM song_revisions WHERE song_id = ? AND revision = ?",
                (song_id, row[0]),
            ).fetchone() if row else None
            unchanged = (row is not None and latest is not None and row[1] == gen.status
                         and SongGeneration.from_record(json.loads(latest[0]), song_id=song_id).to_record() == document)
            if unchanged:
                revision = row[0]
                cur.execute(
                    "UPDATE crafted_songs SET rating = ?, notes = ? WHERE id = ?",
                    (gen.rating, gen.notes, song_id),
                )
            else:
                revision = row[0] + 1 if row else 1
                encoded = json.dumps(document)
                cur.execute(
                    """
                    INSERT INTO crafted_songs (id, revision, status, generation, rating, notes)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (id) DO UPDATE SET
                        revision = excluded.revision,
                        status = excluded.status,
                        generation = excluded.generation,
                        rating = excluded.rating,
                        notes = excluded.notes,
                        updated_at = now()
                    """,
                    (song_id, revision, gen.status, encoded, gen.rating, gen.notes),
                )
                cur.execute(
                    "INSERT INTO song_revisions (song_id, revision, generation, status) VALUES (?, ?, ?, ?)",
                    (song_id, revision, encoded, gen.status),
                )
            row = cur.execute(
                f"SELECT {_SONG_COLUMNS} FROM crafted_songs s WHERE s.id = ?", (song_id,),
            ).fetchone()
            columns = [column[0] for column in cur.description]
        lifecycle = dict(zip(columns, row))
        lifecycle.pop("generation")
        for name, value in lifecycle.items():
            setattr(gen, name, value)
        return gen

    def delete(self, song_ids: list[str]) -> int:
        if not song_ids:
            return 0
        marks = ", ".join("?" * len(song_ids))
        with self.db.transaction() as cur:
            found = cur.execute(f"SELECT count(*) FROM crafted_songs WHERE id IN ({marks})", song_ids).fetchone()[0]
            cur.execute(f"DELETE FROM song_revisions WHERE song_id IN ({marks})", song_ids)
            cur.execute(f"DELETE FROM crafted_songs WHERE id IN ({marks})", song_ids)
        return int(found)

    # ------------------------------------------------------------------ read

    def load(self, song_id: str) -> SongGeneration | None:
        rows = self.db.read_dicts(
            f"SELECT {_SONG_COLUMNS} FROM crafted_songs s WHERE s.id = ?", (song_id,),
        )
        return self._decode(rows[0]) if rows else None

    def revisions(self, song_id: str) -> list[SongGeneration]:
        """Immutable song documents, newest revision first; no reconstruction."""
        rows = self.db.read_dicts(
            f"SELECT {_SONG_COLUMNS} FROM {_REVISION_SOURCE} "
            "WHERE s.id = ? ORDER BY s.revision DESC",
            (song_id,),
        )
        return [self._decode(row) for row in rows]

    def load_revision(self, song_id: str, revision: int) -> SongGeneration | None:
        rows = self.db.read_dicts(
            f"SELECT {_SONG_COLUMNS} FROM {_REVISION_SOURCE} "
            "WHERE s.id = ? AND s.revision = ?",
            (song_id, int(revision)),
        )
        return self._decode(rows[0]) if rows else None

    def list(
        self,
        status: str | None = None,
        genre: str | None = None,
        query: str = "",
        sort_by: str = "updated_at",
        sort_order: str = "desc",
        limit: int = 100,
        offset: int = 0,
    ) -> list[SongGeneration]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if genre:
            clauses.append("genre ILIKE ?")
            params.append(f"%{genre}%")
        if query:
            clauses.append("(title ILIKE ? OR artist ILIKE ? OR brief ILIKE ?)")
            params.extend([f"%{query}%"] * 3)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        order_col = _SORT_COLUMNS.get(sort_by, "updated_at")
        order_dir = "ASC" if str(sort_order).lower() == "asc" else "DESC"
        params.extend([limit, offset])
        rows = self.db.read_dicts(
            f"""
            SELECT {_SONG_COLUMNS}
            FROM crafted_songs s {where}
            ORDER BY {order_col} {order_dir}
            LIMIT ? OFFSET ?
            """,
            params,
        )
        return [self._decode(row) for row in rows]
