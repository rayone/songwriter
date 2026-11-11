"""Fetch artist records from the licensed corpus, never from lyric website scraping."""
from __future__ import annotations

import json
import tempfile
from functools import cache
from typing import Callable
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

import duckdb
from pydantic import ValidationError

from songwriter.corpus.references import ReferenceSongInput
from songwriter.db import duckdb_config

SOURCE_URL = "https://huggingface.co/datasets/theelderemo/genius-lyrics-cleaned"
_EXPORT_ROOT = "https://huggingface.co/api/datasets/theelderemo/genius-lyrics-cleaned/parquet/default/train"
_NON_ALNUM = r"[^\p{L}\p{N}]"


class _LicensedRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlparse(newurl)
        if (target.scheme != "https" or target.hostname not in {"huggingface.co", "us.aws.cdn.hf.co"}
                or target.username is not None or target.port not in (None, 443)):
            raise RuntimeError("The licensed dataset redirected outside its approved HTTPS hosts")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@cache
def _extension_cache() -> tempfile.TemporaryDirectory:
    # The service sets HOME=data/. Keep extension subdirectories out of that flat
    # data root, reuse downloads within this process, and clean up at exit.
    return tempfile.TemporaryDirectory(prefix="songwriter-duckdb-extensions-")


def load_artist(artist: str, timeout_seconds: int = 60,
                progress: Callable[[str], None] | None = None) -> list[ReferenceSongInput]:
    artist = artist.strip()
    if not artist or not 1 <= timeout_seconds <= 300:
        raise ValueError("Artist is required and the request timeout must be 1–300 seconds")
    opener = build_opener(_LicensedRedirects())
    if progress:
        progress("Finding the current licensed dataset exports")
    try:
        with opener.open(Request(_EXPORT_ROOT, headers={"User-Agent": "Songwriter/0.1.0"}), timeout=timeout_seconds) as response:
            exports = json.load(response)
    except Exception as exc:
        raise RuntimeError(f"Could not list the licensed dataset exports: {exc}") from exc
    if not isinstance(exports, list) or not exports:
        raise RuntimeError("The licensed dataset did not return its Parquet export list")
    for url in exports:
        parsed = urlparse(url) if isinstance(url, str) else None
        if (parsed is None or parsed.scheme != "https" or parsed.hostname != "huggingface.co"
                or parsed.username is not None or parsed.port not in (None, 443)
                or not url.startswith(_EXPORT_ROOT + "/") or not parsed.path.endswith(".parquet")):
            raise RuntimeError("The licensed dataset returned an unexpected Parquet export URL")
    if progress:
        progress(f"Locating {len(exports)} licensed dataset files")
    downloads = []
    try:
        for url in exports:
            # Resolve once so Parquet range reads use the approved CDN directly
            # instead of exhausting the Hub API's rate limit on every range.
            with opener.open(Request(url, method="HEAD"), timeout=timeout_seconds) as response:
                downloads.append(response.geturl())
    except Exception as exc:
        raise RuntimeError(f"Could not locate the licensed dataset files: {exc}") from exc
    key = "".join(char for char in artist.lower() if char.isalnum())
    predicate = "regexp_replace(lower(artist), ?, '', 'g') = ?" if key else "lower(trim(artist)) = ?"
    parameters = [downloads, _NON_ALNUM, key] if key else [downloads, artist.lower()]
    try:
        with duckdb.connect(":memory:", config=duckdb_config()) as conn:
            if progress:
                progress("Preparing the licensed dataset network reader")
            conn.execute("SET extension_directory = ?", [_extension_cache().name])
            conn.execute("INSTALL httpfs; LOAD httpfs")
            conn.execute(f"SET http_timeout = {int(timeout_seconds)}; SET http_retries = 0; "
                         "SET allow_asterisks_in_http_paths = true")
            if progress:
                progress(f"Finding matching artist names in all {len(exports)} licensed dataset files")
            names = [
                row[0] for row in conn.execute(
                    f"SELECT DISTINCT artist FROM read_parquet(?) WHERE {predicate}", parameters,
                ).fetchall()
            ]
            if not names:
                return []
            if progress:
                progress(f"Fetching lyrics for {', '.join(names)} from all {len(exports)} dataset files")
            # Filter the artist column first; only matching artists' lyric pages
            # should be transferred, not the multi-gigabyte corpus of other songs.
            placeholders = ", ".join("?" for _ in names)
            rows = conn.execute(
                "SELECT title, artist, tag, year, lyrics FROM read_parquet(?) "
                f"WHERE artist IN ({placeholders}) ORDER BY title", [downloads, *names],
            ).fetchall()
    except duckdb.Error as exc:
        raise RuntimeError(f"Could not read the licensed dataset: {exc}") from exc
    songs = []
    for title, name, genre, year, lyrics in rows:
        try:
            songs.append(ReferenceSongInput(title=title, artist=name, genre=genre or "misc", year=year or 0, lyrics=lyrics))
        except ValidationError as exc:
            error = exc.errors(include_input=False)[0]
            raise RuntimeError(f"Invalid licensed record {title!r}: {error['loc']} — {error['msg']}") from exc
    return songs
