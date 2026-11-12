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

from songwriter.ai.generation import SongGeneration
from songwriter.ai.song_store import SongStore, _SONG_COLUMNS, ensure_schema
from songwriter.db import workbench_db
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status as http_status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from songwriter import config
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

class SongListResponse(BaseModel):
    songs: list[SongGeneration]
    next_cursor: str | None = None

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


def _matches_etag(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    target = etag.strip().removeprefix("W/").strip('"')
    for candidate in if_none_match.split(","):
        cleaned = candidate.strip().removeprefix("W/").strip('"')
        if cleaned == "*" or cleaned == target:
            return True
    return False

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
        "contract_version": 6,
        "total_songs": cnt,
        "storage": {
            "data_dir": str(config.DATA_DIR),
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
    _token: str = Depends(require_auth),
) -> Any:
    store = SongStore()
    clauses: list[str] = []
    params: list[Any] = []

    if status_filter == "ready":
        clauses.append("s.status IN ('ready', 'finalized')")
    elif status_filter != "all":
        clauses.append("s.status = ?")
        params.append(status_filter)

    if genre:
        clauses.append("s.genre ILIKE ?")
        params.append(f"%{genre}%")

    if q:
        clauses.append("(s.title ILIKE ? OR s.brief ILIKE ? OR "
                       "array_to_string(json_extract_string(s.generation, '$.sections[*].lines[*]'), ' ') ILIKE ?)")
        wild = f"%{q}%"
        params.extend([wild, wild, wild])

    if cursor:
        decoded = decode_cursor(cursor)
        if decoded:
            c_time, c_id = decoded
            clauses.append("(s.updated_at < ? OR (s.updated_at = ? AND s.id < ?))")
            params.extend([c_time, c_time, c_id])

    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"""
        SELECT {_SONG_COLUMNS}
        FROM crafted_songs s
        {where_sql}
        ORDER BY s.updated_at DESC, s.id DESC
        LIMIT ?
    """
    params.append(limit + 1)
    records = store.db.read_dicts(sql, params)

    has_more = len(records) > limit
    effective_records = records[:limit]
    songs = [SongStore._decode(rec) for rec in effective_records]

    next_cursor = None
    if has_more and songs:
        last_song = songs[-1]
        last_updated = last_song.updated_at or datetime.now(timezone.utc)
        next_cursor = encode_cursor(last_updated, last_song.song_id)

    result = SongListResponse(songs=songs, next_cursor=next_cursor)
    etag = 'W/"' + hashlib.sha256(result.model_dump_json().encode("utf-8")).hexdigest()[:16] + '"'
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "private, must-revalidate, max-age=5"

    if_none_match = request.headers.get("if-none-match")
    if _matches_etag(if_none_match, etag):
        return Response(status_code=http_status.HTTP_304_NOT_MODIFIED, headers=dict(response.headers))

    return result


@router.get(
    "/songs/{song_id}",
    response_model=SongGeneration,
    summary="Get Full Song Details",
)
def get_song(
    song_id: str,
    request: Request,
    response: Response,
    revision: int | None = Query(None, description="Specific revision to retrieve"),
    _token: str = Depends(require_auth),
) -> Any:
    store = SongStore()
    if revision is not None:
        song = store.load_revision(song_id, revision)
        if not song:
            return error_json(
                status_code=http_status.HTTP_404_NOT_FOUND,
                code="revision_not_found",
                message=f"Revision {revision} of song '{song_id}' not found.",
            )
    else:
        song = store.load(song_id)
        if not song:
            return error_json(
                status_code=http_status.HTTP_404_NOT_FOUND,
                code="song_not_found",
                message=f"Song with ID '{song_id}' not found.",
            )


    body_json = song.model_dump_json()
    etag = 'W/"' + hashlib.sha256(body_json.encode("utf-8")).hexdigest()[:16] + '"'
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "private, must-revalidate, max-age=60"

    if_none_match = request.headers.get("if-none-match")
    if _matches_etag(if_none_match, etag):
        return Response(status_code=http_status.HTTP_304_NOT_MODIFIED, headers=dict(response.headers))

    return song


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
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        file_path = str(config.DATA_DIR / f"upload-{upload_id}.bin")

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
                    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
                    file_key = f"generation-{song_id}_{req.client.generation_uuid}.{ext}"
                    perm_audio_path = config.DATA_DIR / file_key

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
    target = config.DATA_DIR / clean_key
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
