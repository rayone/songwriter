"""One background licensed-catalog import per server process."""
from __future__ import annotations

import threading
import uuid
from typing import Any

from songwriter.config import CORPUS_DB
from songwriter.db import Database
from songwriter.corpus.licensed import load_artist
from songwriter.corpus.references import ReferenceStore, normalize_identity


class ImportInProgress(RuntimeError):
    pass


class ArtistImportManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job: dict[str, Any] | None = None
        self._worker: threading.Thread | None = None

    def status(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._job) if self._job else None

    def start(self, artist: str, store: ReferenceStore, timeout_seconds: int = 60) -> dict[str, Any]:
        with self._lock:
            if self._job and self._job["status"] == "running":
                raise ImportInProgress("An artist import is already running. Follow its progress before starting another.")
            job_id = uuid.uuid4().hex
            self._job = {
                "id": job_id, "status": "running", "artist": artist,
                "phase": "Preparing licensed dataset import", "found": 0,
                "added": 0, "updated": 0, "existing": 0, "total": 0, "song_ids": [], "error": "",
            }
            self._worker = threading.Thread(
                target=self._run, args=(job_id, artist, store, timeout_seconds), daemon=True,
                name="reference-artist-import",
            )
            try:
                self._worker.start()
            except Exception:
                self._job.update(status="failed", error="Could not start the import worker")
                raise
            return dict(self._job)

    def _update(self, job_id: str, **values: Any) -> None:
        with self._lock:
            if self._job and self._job["id"] == job_id:
                self._job.update(values)

    def _run(self, job_id: str, artist: str, store: ReferenceStore, timeout_seconds: int) -> None:
        try:
            songs = load_artist(artist, timeout_seconds, lambda phase: self._update(job_id, phase=phase))
            self._update(job_id, found=len(songs), artist=songs[0].artist if songs else artist,
                         phase=f"Saving {len(songs)} licensed reference songs")
            bundled: dict[tuple[str, str], int] = {}
            if songs and CORPUS_DB.exists():
                artists = list({song.artist.lower() for song in songs})
                bundled = {
                    (normalize_identity(artist), normalize_identity(title)): int(song_id)
                    for song_id, title, artist in Database.get(CORPUS_DB, read_only=True).read(
                        "SELECT song_id, title, artist FROM songs "
                        "WHERE lower(artist) IN (SELECT unnest(?::VARCHAR[]))", [artists],
                    )
                    if song_id is not None and song_id > 0
                }
            overrides = set(store.db.read(
                "SELECT artist_norm, title_norm FROM reference_songs "
                "WHERE artist_norm IN (SELECT unnest(?::VARCHAR[]))",
                [list({normalize_identity(song.artist) for song in songs})],
            )) if bundled else set()
            pending, existing_ids = [], []
            for song in songs:
                identity = (normalize_identity(song.artist), normalize_identity(song.title))
                if identity in bundled and identity not in overrides:
                    existing_ids.append(bundled[identity])
                else:
                    pending.append(song)
            result = store.import_songs(pending, preserve_metadata=True)
            result["song_ids"].extend(existing_ids)
            self._update(job_id, **result, existing=len(existing_ids), status="complete", phase="Import complete")
        except Exception as exc:
            self._update(job_id, status="failed", phase="Import failed", error=str(exc))
