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
    PRIMARY KEY (song_id, revision)
);
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
                _schema_ready.add(key)
    return db


class SongStore:
    """Save / load / list / delete SongGeneration records."""

    def __init__(self, db: Database | None = None) -> None:
        self._db = db

    @property
    def db(self) -> Database:
        return ensure_schema(self._db)

    # ----------------------------------------------------------------- write

    def save(self, gen: SongGeneration) -> SongGeneration:
        """Upsert the song and snapshot the revision in one transaction.

        Assigns ``song_id`` (new songs) and bumps ``revision``; returns the
        stored generation. Saving content identical to the latest revision is a
        no-op that returns that revision, so history only records real changes
        (a draft auto-saves; pressing Save right after must not duplicate it).
        Failures raise -- a save is never silently dropped.
        """
        song_id = gen.song_id or f"song_{uuid.uuid4().hex[:10]}"
        doc = json.dumps(gen.to_record())
        with self.db.transaction() as cur:
            row = cur.execute("SELECT revision FROM crafted_songs WHERE id = ?", (song_id,)).fetchone()
            if row:
                latest = cur.execute(
                    "SELECT generation FROM song_revisions WHERE song_id = ? AND revision = ?",
                    (song_id, row[0]),
                ).fetchone()
                if latest and json.loads(latest[0]) == json.loads(doc):
                    cur.execute("UPDATE crafted_songs SET status = ? WHERE id = ?", (gen.status, song_id))
                    gen.song_id, gen.revision = song_id, row[0]
                    return gen
            revision = (row[0] + 1) if row else 1
            cur.execute(
                """
                INSERT INTO crafted_songs (id, revision, status, generation)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    revision = excluded.revision,
                    status = excluded.status,
                    generation = excluded.generation,
                    updated_at = now()
                """,
                (song_id, revision, gen.status, doc),
            )
            cur.execute(
                "INSERT INTO song_revisions (song_id, revision, generation) VALUES (?, ?, ?)",
                (song_id, revision, doc),
            )
        gen.song_id = song_id
        gen.revision = revision
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
        row = self.db.read_one(
            "SELECT generation, revision, status FROM crafted_songs WHERE id = ?", (song_id,)
        )
        if not row:
            return None
        return SongGeneration.from_record(
            json.loads(row[0]), song_id=song_id, revision=row[1], status=row[2]
        )

    def detail(self, song_id: str) -> dict[str, Any] | None:
        """Flat row (virtual columns + lifecycle) with the full generation attached."""
        rows = self.db.read_dicts(
            """
            SELECT id, revision, status, created_at, updated_at, audio_path, rating, notes,
                   title, artist, brief, genre, subgenre, lyrics, tempo_bpm, musical_key, scale,
                   vocal_style, model_name, abc_score, style_prompt, engine_prompt, engine_target,
                   chords, generation
            FROM crafted_songs WHERE id = ?
            """,
            (song_id,),
        )
        if not rows:
            return None
        out = rows[0]
        out["generation"] = json.loads(out["generation"])
        out["sections"] = out["generation"].get("sections", [])
        return out

    def revisions(self, song_id: str) -> list[dict[str, Any]]:
        """Every saved revision of a song, newest first, with a short preview."""
        return self.db.read_dicts(
            """
            SELECT revision, created_at,
                   generation->>'$.title' AS title,
                   substr(generation->>'$.lyrics', 1, 160) AS lyrics_preview
            FROM song_revisions WHERE song_id = ?
            ORDER BY revision DESC
            """,
            (song_id,),
        )

    def load_revision(self, song_id: str, revision: int) -> SongGeneration | None:
        """The generation exactly as it was saved at ``revision``.

        Its ``song_id`` stays the song's, so saving it again stores a NEW
        revision on top (a restore), never overwrites history.
        """
        row = self.db.read_one(
            "SELECT r.generation, s.status FROM song_revisions r JOIN crafted_songs s ON s.id = r.song_id "
            "WHERE r.song_id = ? AND r.revision = ?",
            (song_id, int(revision)),
        )
        if not row:
            return None
        return SongGeneration.from_record(json.loads(row[0]), song_id=song_id, revision=int(revision), status=row[1])

    def list(
        self,
        status: str | None = None,
        genre: str | None = None,
        query: str = "",
        sort_by: str = "updated_at",
        sort_order: str = "desc",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
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
        return self.db.read_dicts(
            f"""
            SELECT id, revision, title, artist, genre, subgenre, tempo_bpm, musical_key, scale,
                   vocal_style, engine_target, status,
                   (abc_score IS NOT NULL AND abc_score != '') AS has_abc,
                   rating, created_at, updated_at, substr(lyrics, 1, 160) AS lyrics_preview
            FROM crafted_songs {where}
            ORDER BY {order_col} {order_dir}
            LIMIT ? OFFSET ?
            """,
            params,
        )
