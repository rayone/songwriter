"""User-imported references, stored separately from the read-only corpus."""
from __future__ import annotations

import re
import threading
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from songwriter.db import Database, workbench_db

SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS reference_song_id_seq START 1;
CREATE TABLE IF NOT EXISTS reference_songs (
    id BIGINT PRIMARY KEY CHECK (id < 0),
    title VARCHAR NOT NULL,
    artist VARCHAR NOT NULL,
    title_norm VARCHAR NOT NULL,
    artist_norm VARCHAR NOT NULL,
    genre VARCHAR NOT NULL,
    year INTEGER NOT NULL,
    tempo_bpm DOUBLE,
    musical_key VARCHAR,
    scale VARCHAR,
    lyrics VARCHAR NOT NULL,
    line_count INTEGER NOT NULL,
    word_count INTEGER NOT NULL,
    section_count INTEGER NOT NULL,
    updated_at TIMESTAMP DEFAULT current_timestamp,
    UNIQUE (artist_norm, title_norm)
);
"""
_schema_lock = threading.Lock()
_schema_ready: set[str] = set()
_SECTION_RE = re.compile(r"^\[[^\]]+\]$")


def normalize_identity(text: str) -> str:
    return " ".join(text.split()).casefold()


class ReferenceSongInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=240)
    artist: str = Field(min_length=1, max_length=240)
    lyrics: str = Field(min_length=1, max_length=100000)
    genre: str = Field(default="misc", max_length=240)
    year: int = Field(default=0, ge=0, le=9999)
    tempo_bpm: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    musical_key: str | None = Field(default=None, max_length=20)
    scale: str | None = Field(default=None, max_length=40)

    @field_validator("genre", mode="before")
    @classmethod
    def _genre(cls, value: Any) -> Any:
        return (value.strip() or "misc") if isinstance(value, str) else ("misc" if value is None else value)

    @field_validator("musical_key", "scale", mode="before")
    @classmethod
    def _optional_text(cls, value: Any) -> Any:
        return (value.strip() or None) if isinstance(value, str) else value


class ReferenceStore:
    def __init__(self, db: Database | None = None) -> None:
        self._db = db

    @property
    def db(self) -> Database:
        db = self._db or workbench_db()
        key = str(db.path)
        if key not in _schema_ready:
            with _schema_lock:
                if key not in _schema_ready:
                    db.path.parent.mkdir(parents=True, exist_ok=True)
                    with db.transaction() as cur:
                        cur.execute(SCHEMA)
                    _schema_ready.add(key)
        return db

    def count(self) -> int:
        return int(self.db.read_one("SELECT count(*) FROM reference_songs")[0])

    def import_songs(self, songs: list[ReferenceSongInput], *, preserve_metadata: bool = False) -> dict[str, Any]:
        """Replace matching identities atomically; optionally keep known music metadata."""
        identities = [(normalize_identity(s.artist), normalize_identity(s.title)) for s in songs]
        unique = dict(zip(identities, songs))
        ids: dict[tuple[str, str], int] = {}
        added = updated = 0
        with self.db.transaction() as cur:
            for identity, song in unique.items():
                raw_lines = [line.strip() for line in song.lyrics.splitlines() if line.strip()]
                lines = [line for line in raw_lines if not _SECTION_RE.fullmatch(line)]
                if not lines:
                    raise ValueError(f"{song.artist} — {song.title}: lyrics must include more than section headings")
                sections = sum(bool(_SECTION_RE.fullmatch(line)) for line in raw_lines)
                if not sections:
                    sections = len(re.split(r"\n\s*\n", song.lyrics))
                existing = cur.execute(
                    "SELECT id, genre, year, tempo_bpm, musical_key, scale FROM reference_songs "
                    "WHERE artist_norm = ? AND title_norm = ?", identity,
                ).fetchone()
                if existing:
                    if preserve_metadata:
                        known = dict(zip(("genre", "year", "tempo_bpm", "musical_key", "scale"), existing[1:]))
                        song = song.model_copy(update={
                            key: value for key, value in known.items()
                            if value not in (None, "", 0) and not (key == "genre" and value == "misc")
                        })
                    song_id = int(existing[0])
                    updated += 1
                else:
                    song_id = -int(cur.execute("SELECT nextval('reference_song_id_seq')").fetchone()[0])
                    added += 1
                cur.execute("""
                    INSERT INTO reference_songs
                        (id, artist_norm, title_norm, title, artist, lyrics, genre, year,
                         tempo_bpm, musical_key, scale, line_count, word_count, section_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (artist_norm, title_norm) DO UPDATE SET
                        title = excluded.title, artist = excluded.artist, lyrics = excluded.lyrics,
                        genre = excluded.genre, year = excluded.year, tempo_bpm = excluded.tempo_bpm,
                        musical_key = excluded.musical_key, scale = excluded.scale,
                        line_count = excluded.line_count, word_count = excluded.word_count,
                        section_count = excluded.section_count, updated_at = now()
                    """, [song_id, *identity, song.title, song.artist, song.lyrics, song.genre,
                          song.year, song.tempo_bpm, song.musical_key, song.scale, len(lines),
                          sum(len(line.split()) for line in lines), sections])
                ids[identity] = song_id
            total = int(cur.execute("SELECT count(*) FROM reference_songs").fetchone()[0])
        return {"added": added, "updated": updated, "total": total,
                "song_ids": [ids[identity] for identity in identities]}

    def get_song_by_id(self, song_id: int) -> dict[str, Any] | None:
        rows = self.db.read_dicts("SELECT * FROM reference_songs WHERE id = ?", [song_id])
        return rows[0] if rows else None

    def get_song_by_identity(self, title: str | None = None, artist: str | None = None) -> dict[str, Any] | None:
        clauses, params = [], []
        for column, value in (("title_norm", title), ("artist_norm", artist)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(normalize_identity(value))
        if not clauses:
            return None
        rows = self.db.read_dicts(
            f"SELECT * FROM reference_songs WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC, id DESC LIMIT 1",
            params,
        )
        return rows[0] if rows else None

    def search(self, query: str = "", search_by: str = "all", genre: str | None = None,
               year_min: int | None = None, year_max: int | None = None,
               limit: int = 30) -> list[dict[str, Any]]:
        columns = [search_by] if search_by in ("title", "artist", "lyrics") else ["title", "artist", "lyrics"]
        terms = query.strip().split() if search_by == "all" else [query.strip()]
        clauses: list[str] = []
        params: list[Any] = []
        for term in filter(None, terms):
            clauses.append("(" + " OR ".join(f"{column} ILIKE ?" for column in columns) + ")")
            params.extend([f"%{term}%"] * len(columns))
        if genre:
            clauses.append("lower(genre) = lower(?)")
            params.append(genre)
        if year_min is not None:
            clauses.append("year >= ?")
            params.append(year_min)
        if year_max is not None:
            clauses.append("year <= ?")
            params.append(year_max)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        return self.db.read_dicts(f"""
            SELECT *, substr(lyrics, 1, 200) AS preview FROM reference_songs {where}
            ORDER BY CASE WHEN title_norm = ? THEN 0 WHEN contains(artist_norm, ?) THEN 1 ELSE 2 END,
                     length(lyrics) DESC, year DESC, title_norm ASC
            LIMIT ?
            """, [*params, normalize_identity(query), normalize_identity(query), limit])

    def get_recommendation_candidates(self, target_bpm: float, bpm_window: float = 8.0,
                                      genre_terms: list[str] | None = None,
                                      corpus_genre: str = "") -> list[tuple]:
        terms = genre_terms or []
        exact = " OR ".join("contains(lower(genre), ?)" for _ in terms) or "FALSE"
        return self.db.read(f"""
            SELECT id, title, artist, genre, genre AS tag, year, tempo_bpm, musical_key, scale,
                   substr(lyrics, 1, 160), abs(tempo_bpm - ?) AS bpm_diff,
                   CASE WHEN {exact} THEN 2 WHEN lower(genre) = ? THEN 1 ELSE 0 END AS genre_match
            FROM reference_songs
            WHERE tempo_bpm BETWEEN ? AND ? AND length(lyrics) > 60
            ORDER BY genre_match DESC, bpm_diff ASC, id DESC LIMIT 80
            """, [target_bpm, *terms, corpus_genre, target_bpm - bpm_window, target_bpm + bpm_window])
