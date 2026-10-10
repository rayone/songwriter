"""MusicStudio Integration API (v1).

Production REST API implementing the official MusicStudio-Songwriter contract:
- GET  /v1/songs
- GET  /v1/songs/{song_id}
- POST /v1/songs/{song_id}/generations/uploads
- POST /v1/songs/{song_id}/generations
- PUT  /v1/songs/{song_id}/generations/{generation_id}/evaluation
- PUT  /v1/storage/uploads/{upload_id}
- GET  /v1/storage/audio/{file_key}
- GET  /v1/health
"""

from __future__ import annotations

import anyio
import base64
import hashlib
import json
import math
import os
import re
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Literal

import duckdb

from songwriter.ai.song_store import ensure_schema
from songwriter.db import workbench_db
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status as http_status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from songwriter.music.engine_prompt import (
    basic_attributes,
    format_lyrics_for_engine,
    render_minimax,
    render_songbench,
    render_yue2,
)

from songwriter.config import WORKBENCH_DB, CORPUS_DB, AUDIO_DIR
DEFAULT_DB = WORKBENCH_DB
UPLOADS_DIR = AUDIO_DIR / "uploads"
GENERATIONS_DIR = AUDIO_DIR / "generations"
def _ensure_audio_dirs() -> None:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    GENERATIONS_DIR.mkdir(parents=True, exist_ok=True)

router = APIRouter(prefix="/v1", tags=["MusicStudio Integration"])


# ===========================================================================
# Database Helpers & Migration Check
# ===========================================================================

def get_db_con() -> duckdb.DuckDBPyConnection:
    """A cursor on the shared workbench gateway; closing it leaves the database open."""
    ensure_schema()
    return workbench_db().cursor()


get_db_read = get_db_con
get_db_write = get_db_con
def init_db() -> None:
    """Create the MusicStudio render tables; song tables are created by song_store."""
    db = ensure_schema()
    with db.transaction() as cur:
        cur.execute("""

        CREATE TABLE IF NOT EXISTS upload_sessions (
            upload_id VARCHAR PRIMARY KEY,
            song_id VARCHAR NOT NULL,
            song_revision INTEGER NOT NULL,
            idempotency_key VARCHAR,
            content_type VARCHAR NOT NULL,
            size_bytes BIGINT NOT NULL,
            sha256 VARCHAR NOT NULL,
            status VARCHAR DEFAULT 'pending',
            file_path VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL
        );

        CREATE TABLE IF NOT EXISTS song_generations (
            generation_id VARCHAR PRIMARY KEY,
            song_id VARCHAR NOT NULL,
            song_revision INTEGER NOT NULL,
            idempotency_key VARCHAR,
            client_generation_id VARCHAR,
            client_generation_uuid VARCHAR,
            installation_id VARCHAR,
            app_version VARCHAR,
            status VARCHAR NOT NULL,
            model_id VARCHAR NOT NULL,
            model_family VARCHAR NOT NULL,
            seed BIGINT NOT NULL,
            duration_requested_sec DOUBLE NOT NULL,
            duration_actual_sec DOUBLE,
            steps INTEGER NOT NULL,
            guidance DOUBLE NOT NULL,
            cot_mode VARCHAR,
            output_format VARCHAR NOT NULL,
            elapsed_sec DOUBLE,
            audio_upload_id VARCHAR,
            audio_url VARCHAR,
            audio_content_type VARCHAR,
            audio_size_bytes BIGINT,
            audio_sha256 VARCHAR,
            eval_status VARCHAR,
            eval_version VARCHAR,
            eval_device VARCHAR,
            eval_elapsed_sec DOUBLE,
            score_melody DOUBLE,
            score_arrangement DOUBLE,
            score_musicality DOUBLE,
            score_vocal DOUBLE,
            score_instrumental DOUBLE,
            score_mixing DOUBLE,
            score_structure DOUBLE,
            score_overall DOUBLE,
            failure_stage VARCHAR,
            failure_error_code VARCHAR,
            failure_error VARCHAR,
            failure_retryable BOOLEAN,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS idempotency_records (
            key VARCHAR PRIMARY KEY,
            endpoint VARCHAR NOT NULL,
            request_hash VARCHAR NOT NULL,
            status_code INTEGER NOT NULL,
            response_json VARCHAR NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL
        );
        """)


# ===========================================================================
# Authentication & Error Envelope
# ===========================================================================

def error_json(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    retryable: bool = False,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "status": status_code,
                "details": details or {},
                "retryable": retryable,
            }
        },
    )


def require_auth(authorization: str | None = Header(None)) -> str:
    if not authorization:
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": "Missing Authorization header", "retryable": False},
        )
    parts = authorization.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": "Authorization header must be Bearer token", "retryable": False},
        )
    token = parts[1]
    expected = os.environ.get("SONGWRITER_API_KEY")
    if expected and token != expected:
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": "Invalid Bearer token", "retryable": False},
        )
    return token


def to_utc_iso(dt: Any) -> str:
    """Format datetime or string as true UTC ISO-8601 with trailing Z.
    Handles naive local timestamps stored in DuckDB by attaching local timezone
    before converting to UTC, avoiding wrong wall-clock offsets on clients.
    """
    if not dt:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except Exception:
            return dt
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(dt)


# ===========================================================================
# Pydantic Schemas matching contract
# ===========================================================================

class SongListItem(BaseModel):
    id: str
    revision: int
    title: str
    summary: str
    genre: str
    subgenre: str
    language: str = "English"
    instrumental: bool
    duration_hint_sec: int | None = None
    bpm: int | None = None
    key: str | None = None
    scale: str | None = None
    time_signature: str = "4/4"
    has_abc: bool = False
    status: str = "ready"
    updated_at: str
    available_revisions: list[int] = Field(default_factory=list)

class SongListResponse(BaseModel):
    songs: list[SongListItem]
    next_cursor: str | None = None


class CreativeContent(BaseModel):
    caption: str
    lyrics: str
    instrumental: bool
    language: str = "English"
    genre: str
    subgenre: str
    moods: list[str] = Field(default_factory=list)
    instruments: list[str] = Field(default_factory=list)
    bpm: int
    key: str
    scale: str
    time_signature: str = "4/4"
    vocal_style: str = ""
    duration_hint_sec: int | None = None


class MinimaxModelInput(BaseModel):
    caption: str
    lyrics: str


class Yue2ModelInput(BaseModel):
    style: str
    lyrics: str
    abc_score: str | None = None


class ModelInputs(BaseModel):
    minimax: MinimaxModelInput
    yue2: Yue2ModelInput


class SongDetailResponse(BaseModel):
    id: str
    revision: int
    title: str
    summary: str
    status: str
    creative: CreativeContent
    model_inputs: ModelInputs
    created_at: str
    updated_at: str


class UploadRequest(BaseModel):
    song_revision: int
    content_type: str
    size_bytes: int
    sha256: str


class UploadResponse(BaseModel):
    upload_id: str
    method: Literal["PUT"] = "PUT"
    url: str
    headers: dict[str, str]
    expires_at: str


class SongBenchScores(BaseModel):
    melody: float = Field(..., ge=1.0, le=10.0)
    arrangement: float = Field(..., ge=1.0, le=10.0)
    musicality: float = Field(..., ge=1.0, le=10.0)
    vocal: float = Field(..., ge=1.0, le=10.0)
    instrumental: float = Field(..., ge=1.0, le=10.0)
    mixing: float = Field(..., ge=1.0, le=10.0)
    structure: float = Field(..., ge=1.0, le=10.0)


class EvaluationInput(BaseModel):
    status: Literal["completed", "failed", "evaluating", "installing"]
    evaluator_version: str = "songbench-reference-cpu-v1"
    device: str | None = None
    elapsed_sec: float | None = None
    scores: SongBenchScores | None = None
    overall: float | None = None
    error_code: str | None = None
    error: str | None = None
    retryable: bool | None = None


class GenerationConfigInput(BaseModel):
    model_id: str
    model_family: str
    seed: int
    duration_requested_sec: float = Field(..., ge=1.0, le=600.0)
    duration_actual_sec: float | None = None
    steps: int = Field(..., ge=1, le=200)
    guidance: float = Field(default=1.7)
    cot_mode: str | None = None
    output_format: str = "mp3"
    elapsed_sec: float | None = None


class AudioMetadataInput(BaseModel):
    upload_id: str
    url: str | None = None
    content_type: str
    size_bytes: int
    sha256: str


class SourceMetadataInput(BaseModel):
    song_id: str
    song_revision: int


class ClientMetadataInput(BaseModel):
    generation_id: str | None = None
    generation_uuid: str
    installation_id: str | None = None
    app_version: str | None = None


class FailureDetailsInput(BaseModel):
    stage: str
    error_code: str
    error: str
    retryable: bool = True


class GenerationReportRequest(BaseModel):
    source: SourceMetadataInput
    client: ClientMetadataInput
    status: Literal["completed", "failed"]
    completed_at: str | None = None
    failed_at: str | None = None
    generation: GenerationConfigInput
    audio: AudioMetadataInput | None = None
    evaluation: EvaluationInput | None = None
    failure: FailureDetailsInput | None = None


class GenerationReportResponse(BaseModel):
    generation_id: str
    song_id: str
    song_revision: int
    status: str
    audio_url: str | None = None
    evaluation_status: str | None = None
    overall_score: float | None = None
    error_code: str | None = None
    retryable: bool | None = None
    created_at: str


class EvaluationUpdateResponse(BaseModel):
    generation_id: str
    evaluation_status: str
    overall_score: float | None = None
    updated_at: str


# ===========================================================================
# Cursor & Helpers
# ===========================================================================

def encode_cursor(updated_at: datetime, song_id: str) -> str:
    payload = {
        "updated_at": updated_at.isoformat(),
        "id": song_id,
    }
    raw = json.dumps(payload).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor_str: str) -> tuple[datetime, str] | None:
    try:
        raw = base64.urlsafe_b64decode(cursor_str.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
        dt = datetime.fromisoformat(payload["updated_at"])
        return dt, str(payload["id"])
    except Exception:
        return None


def is_instrumental(vocal_style: str, prompt: str = "") -> bool:
    low_v = (vocal_style or "").lower()
    low_p = (prompt or "").lower()
    if "instrumental" in low_v or "no lead vocal" in low_v or "no vocal" in low_v or low_v == "none":
        return True
    if "instrumental" in low_p and "vocal" not in low_p:
        return True
    return False


def estimate_duration_sec(lyrics: str, bpm: float) -> int:
    if not lyrics:
        return 180
    lines = [l.strip() for l in lyrics.splitlines() if l.strip() and not l.startswith("[")]
    word_count = sum(len(l.split()) for l in lines)
    if word_count == 0:
        return 180
    seconds = int(word_count / 2.5) + 30
    return max(60, min(360, seconds))


def build_model_prompts(
    song_dict: dict[str, Any],
) -> tuple[str, str, str, str, str]:
    """Generates minimax caption/lyrics, yue2 style/lyrics, and abc score."""
    lyrics = song_dict.get("lyrics") or ""
    genre = song_dict.get("genre") or "Pop"
    subgenre = song_dict.get("subgenre") or ""
    vocal = song_dict.get("vocal_style") or "melodic lead"
    bpm = int(round(song_dict.get("tempo_bpm") or 118))
    key = song_dict.get("musical_key") or "C"
    scale = song_dict.get("scale") or "major"
    style_prompt = song_dict.get("style_prompt") or ""
    title = song_dict.get("title") or "Untitled Song"

    instrumental = is_instrumental(vocal, style_prompt)

    # 1. MiniMax lyrics & caption
    minimax_lyrics = format_lyrics_for_engine(lyrics, "minimax")
    g_full = genre if not subgenre else f"{genre} ({subgenre})"
    
    # Priority 1: Check if the full rich 3-tier MiniMax caption was already generated & stored with the song
    stored_caption = song_dict.get("engine_prompt") or ""
    if stored_caption and "[Global Metadata]" in stored_caption or ("Global Metadata" in stored_caption and "Vocal Details" in stored_caption and "Arrangement" in stored_caption):
        minimax_caption = stored_caption
    else:
        # Priority 2: Use EnginePromptBuilder with augmentation to construct full 3-tier MiniMax specification
        from songwriter.music.engine_prompt import EnginePromptBuilder
        builder = EnginePromptBuilder(chat=None)
        spec = {
            "genre": g_full,
            "bpm": bpm,
            "music_key": key,
            "scale": scale,
            "vocal": vocal if not instrumental else "instrumental"
        }
        built = builder.build(spec=spec, brief=title, use_model=False)
        minimax_caption = built.get("minimax") or ""
    # 2. YuE2 style & lyrics
    yue2_lyrics = format_lyrics_for_engine(lyrics, "yue2")
    tags = ["English", f"{bpm} BPM", genre]
    if subgenre:
        tags.append(subgenre)
    if instrumental:
        tags.append("instrumental")
    else:
        tags.append(f"{vocal} vocals")
    if style_prompt:
        tags.append(style_prompt)
    yue2_style = render_yue2(tags, bpm)

    # 3. ABC score — only real, imported-from-MIDI scores; never fabricated.
    abc_score = song_dict.get("abc_score") or ""

    return minimax_caption, minimax_lyrics, yue2_style, yue2_lyrics, abc_score


# ===========================================================================
# Endpoints
# ===========================================================================

@router.get("/health", summary="API Health Check")
def health_check() -> dict[str, Any]:
    con = get_db_read()
    try:
        cnt = con.execute("SELECT count(*) FROM crafted_songs").fetchone()[0]
    finally:
        con.close()
    return {
        "status": "healthy",
        "version": "1.2.0",
        "contract_version": 1,
        "total_songs": cnt,
        "storage": {
            "uploads_dir": str(UPLOADS_DIR),
            "generations_dir": str(GENERATIONS_DIR),
        },
    }


@router.get(
    "/songs",
    response_model=SongListResponse,
    summary="List Available Songs for MusicStudio Popover",
)
def list_songs(
    request: Request,
    response: Response,
    status_filter: str = Query("ready", alias="status", description="Status filter: ready, draft, finalized, rendered, all"),
    genre: str | None = Query(None, description="Filter by genre"),
    q: str | None = Query(None, description="Search query across title, brief, lyrics"),
    limit: int = Query(50, ge=1, le=100, description="Max songs to return"),
    cursor: str | None = Query(None, description="Opaque pagination cursor"),
    all_revisions: bool = Query(False, description="List every historical revision as a separate row"),
    _token: str = Depends(require_auth),
) -> Any:
    con = get_db_read()
    try:
        clauses = []
        params: list[Any] = []

        if status_filter == "ready":
            clauses.append("status IN ('ready', 'finalized')")
        elif status_filter != "all":
            clauses.append("status = ?")
            params.append(status_filter)

        if genre:
            clauses.append("genre ILIKE ?")
            params.append(f"%{genre}%")

        if q:
            clauses.append("(title ILIKE ? OR brief ILIKE ? OR lyrics ILIKE ?)")
            wild = f"%{q}%"
            params.extend([wild, wild, wild])

        if cursor:
            decoded = decode_cursor(cursor)
            if decoded:
                c_time, c_id = decoded
                clauses.append("(updated_at < ? OR (updated_at = ? AND id < ?))")
                params.extend([c_time, c_time, c_id])

        where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        if all_revisions:
            sql = """
                SELECT r.song_id, r.revision, r.created_at, r.generation
                FROM song_revisions r
                ORDER BY r.created_at DESC, r.song_id DESC, r.revision DESC
                LIMIT ?
            """
            params = [limit + 1]
            rev_rows = con.execute(sql, params).fetchall()
            effective_rows = rev_rows[:limit]
            has_more = len(rev_rows) > limit
            songs: list[SongListItem] = []
            for r in effective_rows:
                sid, rev, dt, gen_val = r
                g = json.loads(gen_val) if isinstance(gen_val, str) else gen_val
                dt_iso = to_utc_iso(dt)
                vocal = g.get("vocal") or g.get("vocal_style") or ""
                style_prompt = g.get("style_prompt") or ""
                inst = is_instrumental(vocal, style_prompt)
                dur = estimate_duration_sec(g.get("lyrics") or "", int(round(g.get("bpm") or g.get("tempo_bpm") or 118)))
                songs.append(SongListItem(
                    id=sid,
                    revision=rev,
                    title=g.get("title") or "Untitled Song",
                    summary=g.get("brief") or g.get("prompt") or (g.get("lyrics", "")[:100]),
                    genre=g.get("genre") or "Pop",
                    subgenre=g.get("subgenre") or "",
                    language="English",
                    instrumental=inst,
                    duration_hint_sec=dur,
                    bpm=int(round(g.get("bpm") or g.get("tempo_bpm") or 118)),
                    key=f"{g.get('key') or 'C'} {g.get('scale') or 'major'}".strip(),
                    scale=g.get("scale") or "major",
                    time_signature=g.get("time_signature") or "4/4",
                    has_abc=bool(g.get("abc_score")),
                    status="ready",
                    updated_at=dt_iso,
                    available_revisions=[rev],
                ))
            return SongListResponse(songs=songs, next_cursor=None)


        sql = f"""
            SELECT id, COALESCE(revision, 1) AS revision, title, brief, genre, subgenre,
                   tempo_bpm, musical_key, scale, vocal_style, style_prompt, lyrics,
                   (abc_score IS NOT NULL AND trim(abc_score) != '') AS has_abc,
                   status, updated_at
            FROM crafted_songs
            {where_sql}
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
        """
        params.append(limit + 1)
        rows = con.execute(sql, params).fetchall()

        songs: list[SongListItem] = []
        next_cursor = None
        has_more = len(rows) > limit
        effective_rows = rows[:limit]
        rev_map: dict[str, list[int]] = {}
        song_ids = [r[0] for r in effective_rows]
        if song_ids:
            placeholders = ", ".join("?" for _ in song_ids)
            rev_tuples = con.execute(
                f"SELECT song_id, revision FROM song_revisions WHERE song_id IN ({placeholders}) ORDER BY song_id, revision DESC",
                song_ids,
            ).fetchall()
            for sid_rev, r_num in rev_tuples:
                rev_map.setdefault(sid_rev, []).append(r_num)

        hash_parts = []
        for r in effective_rows:
            sid, rev, title, brief, g, sg, bpm, key, scale, vocal, prompt, lyrics, has_abc, stat, updated_at = r
            dt_iso = to_utc_iso(updated_at)
            hash_parts.append(f"{sid}:{rev}:{dt_iso}")

            inst = is_instrumental(vocal, prompt)
            duration_hint = estimate_duration_sec(lyrics, bpm or 118)
            has_abc_available = bool(has_abc)

            songs.append(SongListItem(
                id=sid,
                revision=rev,
                title=title or "Untitled Song",
                summary=brief or prompt or (lyrics[:100] if lyrics else ""),
                genre=g or "Pop",
                subgenre=sg or "",
                language="English",
                instrumental=inst,
                duration_hint_sec=duration_hint,
                bpm=int(round(bpm or 118)),
                key=f"{key} {scale}".strip() if key else "C major",
                scale=scale or "major",
                time_signature="4/4",
                has_abc=has_abc_available,
                status=stat or "ready",
                updated_at=dt_iso,
                available_revisions=rev_map.get(sid, [rev]),
            ))
        if has_more:
            last_row = effective_rows[-1]
            last_id = last_row[0]
            last_updated = last_row[14] or datetime.now(timezone.utc)
            next_cursor = encode_cursor(last_updated, last_id)

        # Compute ETag
        etag = 'W/"' + hashlib.sha256(";".join(hash_parts).encode("utf-8")).hexdigest()[:16] + '"'
        response.headers["ETag"] = etag
        response.headers["Cache-Control"] = "private, must-revalidate, max-age=5"

        if_none_match = request.headers.get("if-none-match")
        if if_none_match and if_none_match == etag:
            return Response(status_code=http_status.HTTP_304_NOT_MODIFIED)

        return SongListResponse(songs=songs, next_cursor=next_cursor)
    finally:
        con.close()


@router.get(
    "/songs/{song_id}",
    response_model=SongDetailResponse,
    summary="Get Full Song Details & Pre-Formatted Model Inputs",
)
def get_song(
    song_id: str,
    request: Request,
    response: Response,
    revision: int | None = Query(None, description="Specific revision to retrieve"),
    _token: str = Depends(require_auth),
) -> Any:
    con = get_db_read()
    try:
        if revision is not None:
            rev_row = con.execute(
                "SELECT generation, created_at FROM song_revisions WHERE song_id = ? AND revision = ?",
                (song_id, revision),
            ).fetchone()
            if not rev_row:
                return error_json(
                    status_code=http_status.HTTP_404_NOT_FOUND,
                    code="revision_not_found",
                    message=f"Revision {revision} of song '{song_id}' not found.",
                )
            gen = json.loads(rev_row[0]) if isinstance(rev_row[0], str) else rev_row[0]
            created_at = rev_row[1]
            rev = revision
            title = gen.get("title") or "Untitled Song"
            brief = gen.get("brief") or gen.get("prompt") or ""
            genre = gen.get("genre") or "Pop"
            subgenre = gen.get("subgenre") or ""
            vocal = gen.get("vocal") or gen.get("vocal_style") or "melodic lead"
            style_prompt = gen.get("style_prompt") or ""
            lyrics = gen.get("lyrics") or ""
            bpm = int(round(gen.get("bpm") or gen.get("tempo_bpm") or 118))
            key = gen.get("key") or gen.get("musical_key") or "C"
            scale = gen.get("scale") or "major"
            stat = "ready"
            created_iso = to_utc_iso(created_at)
            updated_iso = created_iso
            song_dict = {
                "title": title, "brief": brief, "genre": genre, "subgenre": subgenre,
                "vocal_style": vocal, "style_prompt": style_prompt, "lyrics": lyrics,
                "tempo_bpm": bpm, "musical_key": key, "scale": scale,
                "engine_prompt": (gen.get("engine_prompts") or {}).get("minimax"),
                "abc_score": gen.get("abc_score"),
            }
        else:
            row = con.execute("SELECT * FROM crafted_songs WHERE id = ?", (song_id,)).fetchone()
            if not row:
                return error_json(
                    status_code=http_status.HTTP_404_NOT_FOUND,
                    code="song_not_found",
                    message=f"Song with ID '{song_id}' not found.",
                )

            cols = [c[0] for c in con.description]
            song_dict = dict(zip(cols, row))

            rev = song_dict.get("revision") or 1
            title = song_dict.get("title") or "Untitled Song"
            brief = song_dict.get("brief") or ""
            genre = song_dict.get("genre") or "Pop"
            subgenre = song_dict.get("subgenre") or ""
            vocal = song_dict.get("vocal_style") or "melodic lead"
            style_prompt = song_dict.get("style_prompt") or ""
            lyrics = song_dict.get("lyrics") or ""
            bpm = int(round(song_dict.get("tempo_bpm") or 118))
            key = song_dict.get("musical_key") or "C"
            scale = song_dict.get("scale") or "major"
            stat = song_dict.get("status") or "ready"
            created_at = song_dict.get("created_at")
            updated_at = song_dict.get("updated_at")

            created_iso = to_utc_iso(created_at)
            updated_iso = to_utc_iso(updated_at)

        etag = f'"{song_id}:{rev}"'
        response.headers["ETag"] = etag
        response.headers["Cache-Control"] = "private, must-revalidate, max-age=60"

        if_none_match = request.headers.get("if-none-match")
        if if_none_match and if_none_match == etag:
            return Response(status_code=http_status.HTTP_304_NOT_MODIFIED)
        inst = is_instrumental(vocal, style_prompt)
        duration_hint = estimate_duration_sec(lyrics, bpm)

        # Build model prompts and ABC score
        mm_caption, mm_lyrics, yue2_style, yue2_lyrics, abc_score = build_model_prompts(song_dict)

        # Generic caption
        caption = style_prompt or mm_caption.split("\n\n")[0].replace("[Global Metadata]\n", "")

        return SongDetailResponse(
            id=song_id,
            revision=rev,
            title=title,
            summary=brief,
            status=stat,
            creative=CreativeContent(
                caption=caption,
                lyrics=lyrics,
                instrumental=inst,
                language="English",
                genre=genre,
                subgenre=subgenre,
                moods=[],
                instruments=[],
                bpm=bpm,
                key=f"{key} {scale}".strip(),
                scale=scale,
                time_signature="4/4",
                vocal_style=vocal,
                duration_hint_sec=duration_hint,
            ),
            model_inputs=ModelInputs(
                minimax=MinimaxModelInput(caption=mm_caption, lyrics=mm_lyrics),
                yue2=Yue2ModelInput(style=yue2_style, lyrics=yue2_lyrics, abc_score=abc_score),
            ),
            created_at=created_iso,
            updated_at=updated_iso,
        )
    finally:
        con.close()


@router.post(
    "/songs/{song_id}/generations/uploads",
    response_model=UploadResponse,
    status_code=http_status.HTTP_201_CREATED,
    summary="Allocate Audio Upload Ticket",
)
def request_upload(
    song_id: str,
    req: UploadRequest,
    request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    _token: str = Depends(require_auth),
) -> Any:
    con = get_db_con()
    try:
        # Check idempotency
        if idempotency_key:
            existing = con.execute(
                "SELECT status_code, response_json FROM idempotency_records WHERE key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                code, resp_text = existing
                return JSONResponse(status_code=code, content=json.loads(resp_text))

        # Check song exists and revision matches
        row = con.execute(
            "SELECT COALESCE(revision, 1) FROM crafted_songs WHERE id = ?",
            (song_id,),
        ).fetchone()
        if not row:
            return error_json(
                status_code=http_status.HTTP_404_NOT_FOUND,
                code="song_not_found",
                message=f"Song with ID '{song_id}' not found.",
            )

        current_rev = row[0]
        if req.song_revision != current_rev:
            return error_json(
                status_code=http_status.HTTP_409_CONFLICT,
                code="stale_revision",
                message=f"Provided song revision {req.song_revision} does not match current revision {current_rev}.",
                details={"song_id": song_id, "current_revision": current_rev, "provided_revision": req.song_revision},
            )

        # Validate mime type
        valid_mimes = {"audio/mpeg", "audio/wav", "audio/x-wav", "audio/flac", "audio/mp4", "audio/x-m4a"}
        if req.content_type.lower() not in valid_mimes:
            return error_json(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                code="invalid_content_type",
                message=f"Content-Type '{req.content_type}' is not supported. Supported: {', '.join(sorted(valid_mimes))}.",
            )

        # Validate sha256
        if not re.match(r"^[a-f0-9]{64}$", req.sha256.lower()):
            return error_json(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                code="invalid_checksum",
                message="SHA-256 digest must be a 64-character lowercase hex string.",
            )

        # Allocate upload session
        upload_id = f"upload_{uuid.uuid4().hex}"
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
        expires_iso = expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        _ensure_audio_dirs()
        file_path = str(UPLOADS_DIR / f"{upload_id}.bin")

        con.execute("""
            INSERT INTO upload_sessions (
                upload_id, song_id, song_revision, idempotency_key, content_type,
                size_bytes, sha256, status, file_path, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, CURRENT_TIMESTAMP, ?)
        """, (
            upload_id, song_id, req.song_revision, idempotency_key, req.content_type,
            req.size_bytes, req.sha256.lower(), file_path, expires_at
        ))

        # Base URL construction
        base_url = str(request.base_url).rstrip("/")
        upload_url = f"{base_url}/v1/storage/uploads/{upload_id}"

        resp_data = {
            "upload_id": upload_id,
            "method": "PUT",
            "url": upload_url,
            "headers": {"Content-Type": req.content_type},
            "expires_at": expires_iso,
        }

        if idempotency_key:
            con.execute("""
                INSERT INTO idempotency_records (key, endpoint, request_hash, status_code, response_json, created_at, expires_at)
                VALUES (?, '/v1/songs/generations/uploads', ?, 201, ?, CURRENT_TIMESTAMP, ?)
            """, (
                idempotency_key,
                hashlib.sha256(json.dumps(req.model_dump()).encode()).hexdigest(),
                json.dumps(resp_data),
                expires_at + timedelta(hours=24),
            ))

        return UploadResponse(**resp_data)
    finally:
        con.close()

def _get_upload_session(upload_id: str) -> tuple | None:
    con = get_db_con()
    try:
        return con.execute(
            "SELECT song_id, song_revision, content_type, size_bytes, sha256, status, expires_at, file_path "
            "FROM upload_sessions WHERE upload_id = ?",
            (upload_id,),
        ).fetchone()
    finally:
        con.close()


def _mark_upload_complete(upload_id: str) -> None:
    con = get_db_con()
    try:
        con.execute(
            "UPDATE upload_sessions SET status = 'uploaded' WHERE upload_id = ?",
            (upload_id,),
        )
    finally:
        con.close()


@router.put(
    "/storage/uploads/{upload_id}",
    summary="Direct Binary Audio Upload",
)
async def upload_audio_binary(
    upload_id: str,
    request: Request,
) -> Any:
    session = await anyio.to_thread.run_sync(_get_upload_session, upload_id)
    if not session:
        return error_json(
            status_code=http_status.HTTP_404_NOT_FOUND,
            code="upload_not_found",
            message=f"Upload session '{upload_id}' not found.",
        )

    sid, rev, content_type, expected_size, expected_sha, upload_stat, expires_at, file_path = session

    if datetime.now(timezone.utc) > expires_at.replace(tzinfo=timezone.utc):
        return error_json(
            status_code=http_status.HTTP_410_GONE,
            code="upload_expired",
            message="Upload session has expired. Please allocate a new upload ticket.",
        )

    target_path = Path(file_path)
    sha = hashlib.sha256()
    bytes_received = 0

    async with await anyio.open_file(target_path, "wb") as f:
        async for chunk in request.stream():
            if chunk:
                bytes_received += len(chunk)
                if bytes_received > expected_size:
                    break
                await f.write(chunk)
                sha.update(chunk)
    actual_sha = sha.hexdigest().lower()

    if bytes_received != expected_size:
        target_path.unlink(missing_ok=True)
        return error_json(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            code="size_mismatch",
            message=f"Expected {expected_size} bytes but received {bytes_received} bytes.",
        )

    if actual_sha != expected_sha:
        target_path.unlink(missing_ok=True)
        return error_json(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            code="checksum_mismatch",
            message=f"SHA-256 digest {actual_sha} does not match expected {expected_sha}.",
        )

    await anyio.to_thread.run_sync(_mark_upload_complete, upload_id)
    return {"status": "uploaded", "upload_id": upload_id, "size_bytes": bytes_received, "sha256": actual_sha}


@router.post(
    "/songs/{song_id}/generations",
    response_model=GenerationReportResponse,
    status_code=http_status.HTTP_201_CREATED,
    summary="Report Completed or Failed Generation and Evaluation",
)
def report_generation(
    song_id: str,
    req: GenerationReportRequest,
    request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    _token: str = Depends(require_auth),
) -> Any:
    con = get_db_con()
    try:
        # Check idempotency
        if idempotency_key:
            existing = con.execute(
                "SELECT status_code, response_json FROM idempotency_records WHERE key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                code, resp_text = existing
                return JSONResponse(status_code=code, content=json.loads(resp_text))

        # Check song exists and revision
        row = con.execute("SELECT COALESCE(revision, 1) FROM crafted_songs WHERE id = ?", (song_id,)).fetchone()
        if not row:
            return error_json(
                status_code=http_status.HTTP_404_NOT_FOUND,
                code="song_not_found",
                message=f"Song with ID '{song_id}' not found.",
            )

        current_rev = row[0]
        if req.source.song_revision != current_rev:
            # Check if this revision existed in revision history
            rev_history = con.execute(
                "SELECT 1 FROM song_revisions WHERE song_id = ? AND revision = ?",
                (song_id, req.source.song_revision),
            ).fetchone()
            if not rev_history and req.source.song_revision > current_rev:
                return error_json(
                    status_code=http_status.HTTP_409_CONFLICT,
                    code="stale_revision",
                    message=f"Revision {req.source.song_revision} does not exist.",
                    details={"song_id": song_id, "current_revision": current_rev, "reported_revision": req.source.song_revision},
                )

        gen_id = f"gen_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)
        now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        base_url = str(request.base_url).rstrip("/")

        if req.status == "completed":
            audio_url = None
            audio_upload_id = None
            audio_content_type = None
            audio_size_bytes = None
            audio_sha256 = None

            if req.audio:
                # Validate upload exists and was finalized if audio was provided
                upload_session = con.execute(
                    "SELECT status, file_path FROM upload_sessions WHERE upload_id = ?",
                    (req.audio.upload_id,),
                ).fetchone()
                if upload_session and upload_session[0] == "uploaded":
                    ext = req.generation.output_format.lower().strip(".")
                    _ensure_audio_dirs()
                    file_key = f"{song_id}_{req.client.generation_uuid}.{ext}"
                    perm_audio_path = GENERATIONS_DIR / file_key

                    src_path = Path(upload_session[1])
                    if src_path.exists():
                        src_path.replace(perm_audio_path)

                    audio_url = f"{base_url}/v1/storage/audio/{file_key}"
                    audio_upload_id = req.audio.upload_id
                    audio_content_type = req.audio.content_type
                    audio_size_bytes = req.audio.size_bytes
                    audio_sha256 = req.audio.sha256
                    con.execute(
                        "UPDATE crafted_songs SET audio_path = ? WHERE id = ?",
                        (str(perm_audio_path), song_id),
                    )
            # Validate SongBench evaluation if completed
            overall_score = None
            eval_status = req.evaluation.status if req.evaluation else "pending"
            scores = req.evaluation.scores if req.evaluation else None

            if req.evaluation and req.evaluation.status == "completed":
                if not scores:
                    return error_json(
                        status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                        code="missing_scores",
                        message="Completed evaluation must include all 7 scores.",
                    )
                all_7 = [
                    scores.melody,
                    scores.arrangement,
                    scores.musicality,
                    scores.vocal,
                    scores.instrumental,
                    scores.mixing,
                    scores.structure,
                ]
                computed_overall = sum(all_7) / 7.0
                if req.evaluation.overall is not None and abs(req.evaluation.overall - computed_overall) > 0.05:
                    return error_json(
                        status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                        code="invalid_overall_score",
                        message=f"Provided overall {req.evaluation.overall} does not match arithmetic mean {computed_overall:.6f}.",
                    )
                overall_score = round(computed_overall, 6)

            # Insert into song_generations
            con.execute("""
                INSERT INTO song_generations (
                    generation_id, song_id, song_revision, idempotency_key,
                    client_generation_id, client_generation_uuid, installation_id, app_version,
                    status, model_id, model_family, seed, duration_requested_sec, duration_actual_sec,
                    steps, guidance, cot_mode, output_format, elapsed_sec,
                    audio_upload_id, audio_url, audio_content_type, audio_size_bytes, audio_sha256,
                    eval_status, eval_version, eval_device, eval_elapsed_sec,
                    score_melody, score_arrangement, score_musicality, score_vocal, score_instrumental,
                    score_mixing, score_structure, score_overall, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
            """, (
                gen_id, song_id, req.source.song_revision, idempotency_key,
                req.client.generation_id, req.client.generation_uuid, req.client.installation_id, req.client.app_version,
                "completed", req.generation.model_id, req.generation.model_family, req.generation.seed,
                req.generation.duration_requested_sec, req.generation.duration_actual_sec,
                req.generation.steps, req.generation.guidance, req.generation.cot_mode,
                req.generation.output_format, req.generation.elapsed_sec,
                audio_upload_id, audio_url, audio_content_type, audio_size_bytes, audio_sha256,
                eval_status,
                req.evaluation.evaluator_version if req.evaluation else None,
                req.evaluation.device if req.evaluation else None,
                req.evaluation.elapsed_sec if req.evaluation else None,
                scores.melody if scores else None,
                scores.arrangement if scores else None,
                scores.musicality if scores else None,
                scores.vocal if scores else None,
                scores.instrumental if scores else None,
                scores.mixing if scores else None,
                scores.structure if scores else None,
                overall_score,
            ))

            # Update song status
            con.execute(
                "UPDATE crafted_songs SET status = 'rendered' WHERE id = ?",
                (song_id,),
            )

            resp_payload = {
                "generation_id": gen_id,
                "song_id": song_id,
                "song_revision": req.source.song_revision,
                "status": "completed",
                "audio_url": audio_url,
                "evaluation_status": eval_status,
                "overall_score": overall_score,
                "created_at": now_iso,
            }

        else:
            # Failed generation
            fail = req.failure or FailureDetailsInput(
                stage="generation", error_code="unknown_error", error="Unknown failure", retryable=True
            )
            con.execute("""
                INSERT INTO song_generations (
                    generation_id, song_id, song_revision, idempotency_key,
                    client_generation_id, client_generation_uuid, installation_id, app_version,
                    status, model_id, model_family, seed, duration_requested_sec,
                    steps, guidance, cot_mode, output_format,
                    failure_stage, failure_error_code, failure_error, failure_retryable,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    'failed', ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
            """, (
                gen_id, song_id, req.source.song_revision, idempotency_key,
                req.client.generation_id, req.client.generation_uuid, req.client.installation_id, req.client.app_version,
                req.generation.model_id, req.generation.model_family, req.generation.seed,
                req.generation.duration_requested_sec, req.generation.steps,
                req.generation.guidance, req.generation.cot_mode, req.generation.output_format,
                fail.stage, fail.error_code, fail.error, fail.retryable,
            ))

            resp_payload = {
                "generation_id": gen_id,
                "song_id": song_id,
                "song_revision": req.source.song_revision,
                "status": "failed",
                "error_code": fail.error_code,
                "retryable": fail.retryable,
                "created_at": now_iso,
            }

        if idempotency_key:
            con.execute("""
                INSERT INTO idempotency_records (key, endpoint, request_hash, status_code, response_json, created_at, expires_at)
                VALUES (?, '/v1/songs/generations', ?, 201, ?, CURRENT_TIMESTAMP, ?)
            """, (
                idempotency_key,
                hashlib.sha256(json.dumps(req.model_dump()).encode()).hexdigest(),
                json.dumps(resp_payload),
                now + timedelta(hours=24),
            ))

        return GenerationReportResponse(**resp_payload)
    finally:
        con.close()


@router.put(
    "/songs/{song_id}/generations/{generation_id}/evaluation",
    response_model=EvaluationUpdateResponse,
    summary="Update SongBench Evaluation Scores for a Completed Generation",
)
def update_evaluation(
    song_id: str,
    generation_id: str,
    req: EvaluationInput,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    _token: str = Depends(require_auth),
) -> Any:
    con = get_db_con()
    try:
        gen_row = con.execute(
            "SELECT song_id, status FROM song_generations WHERE generation_id = ?",
            (generation_id,),
        ).fetchone()

        if not gen_row:
            return error_json(
                status_code=http_status.HTTP_404_NOT_FOUND,
                code="generation_not_found",
                message=f"Generation '{generation_id}' not found.",
            )

        if gen_row[0] != song_id:
            return error_json(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                code="song_mismatch",
                message=f"Generation '{generation_id}' belongs to song '{gen_row[0]}', not '{song_id}'.",
            )

        now = datetime.now(timezone.utc)
        now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        overall_score = None
        if req.status == "completed":
            if not req.scores:
                return error_json(
                    status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                    code="missing_scores",
                    message="Completed evaluation update must include all 7 scores.",
                )
            all_7 = [
                req.scores.melody,
                req.scores.arrangement,
                req.scores.musicality,
                req.scores.vocal,
                req.scores.instrumental,
                req.scores.mixing,
                req.scores.structure,
            ]
            computed_overall = sum(all_7) / 7.0
            overall_score = round(computed_overall, 6)

            con.execute("""
                UPDATE song_generations SET
                    eval_status = 'completed',
                    eval_version = ?,
                    eval_device = ?,
                    eval_elapsed_sec = ?,
                    score_melody = ?,
                    score_arrangement = ?,
                    score_musicality = ?,
                    score_vocal = ?,
                    score_instrumental = ?,
                    score_mixing = ?,
                    score_structure = ?,
                    score_overall = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE generation_id = ?
            """, (
                req.evaluator_version,
                req.device,
                req.elapsed_sec,
                req.scores.melody,
                req.scores.arrangement,
                req.scores.musicality,
                req.scores.vocal,
                req.scores.instrumental,
                req.scores.mixing,
                req.scores.structure,
                overall_score,
                generation_id,
            ))
        else:
            con.execute("""
                UPDATE song_generations SET
                    eval_status = ?,
                    eval_version = ?,
                    eval_elapsed_sec = ?,
                    failure_error_code = ?,
                    failure_error = ?,
                    failure_retryable = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE generation_id = ?
            """, (
                req.status,
                req.evaluator_version,
                req.elapsed_sec,
                req.error_code,
                req.error,
                req.retryable,
                generation_id,
            ))

        return EvaluationUpdateResponse(
            generation_id=generation_id,
            evaluation_status=req.status,
            overall_score=overall_score,
            updated_at=now_iso,
        )
    finally:
        con.close()


@router.get(
    "/storage/audio/{file_key}",
    summary="Download or Stream Generated Audio File",
)
def stream_audio(file_key: str) -> Any:
    clean_key = Path(file_key).name
    target = GENERATIONS_DIR / clean_key
    if not target.exists() or not target.is_file():
        return error_json(
            status_code=http_status.HTTP_404_NOT_FOUND,
            code="audio_not_found",
            message=f"Audio file '{clean_key}' not found.",
        )

    ext = target.suffix.lower()
    media_types = {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
    }
    media_type = media_types.get(ext, "application/octet-stream")

    return FileResponse(
        path=str(target),
        media_type=media_type,
        filename=clean_key,
        headers={"Accept-Ranges": "bytes"},
    )
