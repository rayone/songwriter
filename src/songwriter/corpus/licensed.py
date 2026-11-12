"""Fetch artist records from the licensed corpus, never from lyric website scraping."""
from __future__ import annotations

import http.client
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

import pyarrow.compute as pc
import pyarrow.parquet as pq
from pydantic import ValidationError

from songwriter.corpus.references import ReferenceSongInput

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


class _RemoteParquet(io.RawIOBase):
    """Seekable, bounded HTTP range reads with one reusable connection per file."""

    def __init__(self, url: str, size: int, timeout_seconds: int) -> None:
        super().__init__()
        parsed = urlparse(url)
        self._path = parsed.path + ("?" + parsed.query if parsed.query else "")
        self._size = size
        self._position = 0
        self._connection = http.client.HTTPSConnection(parsed.hostname, timeout=timeout_seconds)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_CUR:
            offset += self._position
        elif whence == io.SEEK_END:
            offset += self._size
        elif whence != io.SEEK_SET:
            raise ValueError("Invalid seek origin")
        if offset < 0:
            raise ValueError("Cannot seek before the file")
        self._position = offset
        return offset

    def read(self, size: int = -1) -> bytes:
        remaining = max(0, self._size - self._position)
        size = remaining if size < 0 else min(size, remaining)
        if not size:
            return b""
        start = self._position
        end = start + size - 1
        self._connection.request("GET", self._path, headers={
            "Range": f"bytes={start}-{end}", "Accept-Encoding": "identity",
            "User-Agent": "Songwriter/0.1.0",
        })
        response = self._connection.getresponse()
        try:
            if response.status != 206 or response.getheader("Content-Range") != f"bytes {start}-{end}/{self._size}":
                raise RuntimeError("The licensed source did not honor a byte-range request")
            data = response.read(size)
            if len(data) != size:
                raise RuntimeError("The licensed source returned an incomplete file range")
        finally:
            response.close()
        self._position += size
        return data

    def close(self) -> None:
        self._connection.close()
        super().close()


def _read_artist_file(url: str, artist: str, key: str, timeout_seconds: int,
                      report: Callable[[int, int, bool], None],
                      stopped: threading.Event) -> list[ReferenceSongInput]:
    opener = build_opener(_LicensedRedirects())
    with opener.open(Request(url, method="HEAD"), timeout=timeout_seconds) as response:
        resolved = response.geturl()
        size = int(response.headers["Content-Length"])
    songs = []
    with _RemoteParquet(resolved, size, timeout_seconds) as source, pq.ParquetFile(source) as parquet:
        for group in range(parquet.num_row_groups):
            if stopped.is_set():
                return []
            names = parquet.read_row_group(group, columns=["artist"], use_threads=False).column("artist")
            normalized = pc.utf8_lower(names)
            normalized = (pc.replace_substring_regex(normalized, _NON_ALNUM, "") if key
                          else pc.utf8_trim_whitespace(normalized))
            matching = pc.indices_nonzero(pc.equal(normalized, key or artist.lower()))
            if len(matching):
                # Only row groups containing this artist need their lyric columns.
                records = parquet.read_row_group(
                    group, columns=["title", "tag", "year", "lyrics"], use_threads=False,
                ).take(matching)
                records = records.append_column("artist", names.take(matching))
                for record in records.to_pylist():
                    try:
                        songs.append(ReferenceSongInput(
                            title=record["title"], artist=record["artist"], genre=record["tag"] or "misc",
                            year=record["year"] or 0, lyrics=record["lyrics"],
                        ))
                    except ValidationError as exc:
                        error = exc.errors(include_input=False)[0]
                        raise RuntimeError(
                            f"Invalid licensed record {record['title']!r}: {error['loc']} — {error['msg']}"
                        ) from exc
            report(len(names), len(matching), False)
    report(0, 0, True)
    return songs


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

    key = "".join(char for char in artist.lower() if char.isalnum())
    lock = threading.Lock()
    stopped = threading.Event()
    checked = found = completed = 0

    def report(rows: int, matches: int, finished: bool) -> None:
        nonlocal checked, found, completed
        if progress is None:
            return
        with lock:
            checked += rows
            found += matches
            completed += int(finished)
            progress(f"Checked {checked:,} reference records; {found} matching songs; "
                     f"{completed}/{len(exports)} files complete")

    if progress:
        progress(f"Searching artist records in {len(exports)} licensed dataset files")
    results: list[list[ReferenceSongInput]] = [[] for _ in exports]
    # These are I/O workers, independent of the main database's CPU thread limit.
    with ThreadPoolExecutor() as workers:
        pending = {
            workers.submit(_read_artist_file, url, artist, key, timeout_seconds, report, stopped): index
            for index, url in enumerate(exports)
        }
        try:
            for future in as_completed(pending):
                index = pending[future]
                results[index] = future.result()
        except Exception as exc:
            stopped.set()
            for future in pending:
                future.cancel()
            raise RuntimeError(f"Could not read licensed dataset file {index + 1}/{len(exports)}: {exc}") from exc
    return sorted((song for records in results for song in records), key=lambda song: song.title)
