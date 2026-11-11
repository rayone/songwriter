"""Imported references retain identity, filters, and honest musical metadata."""
from __future__ import annotations

import importlib
import threading
import subprocess
import sys
import textwrap

import duckdb

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from songwriter.ai.cowriter import SongwriterCowriter
from songwriter.corpus.references import ReferenceSongInput, ReferenceStore
from songwriter.corpus.search import SongSearcher
from songwriter.corpus.artist_imports import ArtistImportManager, ImportInProgress
from songwriter.db import Database
from songwriter.service import Workbench


LYRICS = "[Verse]\nA lantern in the window glows\nThe last train takes the long way home\n\n[Chorus]\nLeave one light above the door\nI will find the road once more"


def reference(**changes):
    return ReferenceSongInput.model_validate({
        "title": "City Windows", "artist": "Example Artist", "lyrics": LYRICS,
        "genre": "darkwave", "year": 2023, "tempo_bpm": 125,
        "musical_key": "F#", "scale": "minor", **changes,
    })


@pytest.fixture
def catalog(tmp_path):
    store = ReferenceStore(Database.get(tmp_path / "workbench.duckdb", read_only=False))
    searcher = SongSearcher(
        parquet_glob=tmp_path / "songs.parquet",
        sqlite_path=tmp_path / "absent.duckdb", reference_store=store,
    )
    return store, searcher


def test_identity_survives_normalized_updates_and_new_searchers(catalog):
    store, searcher = catalog
    first = store.import_songs([reference()])
    song_id = first["song_ids"][0]
    assert song_id < 0
    assert searcher.get_song(song_id=song_id).sections[0].kind == "verse"

    changed_lyrics = LYRICS.replace("lantern", "candle")
    updated = store.import_songs([reference(
        title="  CITY   WINDOWS ", artist="example artist ",
        lyrics=changed_lyrics, tempo_bpm=None, musical_key=None, scale=None,
    )])
    assert updated == {"added": 0, "updated": 1, "total": 1, "song_ids": [song_id]}
    assert searcher.get_song(song_id=song_id).raw_lyrics == changed_lyrics
    reopened = SongSearcher(
        parquet_glob=searcher.parquet_glob, sqlite_path=searcher.sqlite_path,
        reference_store=ReferenceStore(store.db),
    )
    song = reopened.get_song(title="city windows", artist="Example Artist")
    assert song.song_id == song_id
    assert (song.tempo_bpm, song.musical_key, song.scale) == (None, None, None)


def test_repeated_batch_identity_counts_once_and_last_record_wins(catalog):
    store, searcher = catalog
    result = store.import_songs([
        reference(), reference(title="CITY WINDOWS", tempo_bpm=128),
    ])
    assert (result["added"], result["updated"], result["total"]) == (1, 0, 1)
    assert result["song_ids"][0] == result["song_ids"][1]
    assert searcher.get_song(song_id=result["song_ids"][0]).tempo_bpm == 128


@pytest.mark.parametrize(("filters", "titles"), [
    ({"query": "city example"}, {"City Windows"}),
    ({"query": "city", "search_by": "artist"}, set()),
    ({"query": "lantern", "search_by": "lyrics"}, {"City Windows"}),
    ({"genre": "darkwave", "year_min": 2022, "year_max": 2024}, {"City Windows"}),
    ({"genre": "darkwave"}, {"City Windows"}),
    ({"year_max": 2021}, {"Open Water"}),
    ({"has_midi": True}, set()),
])
def test_import_search_obeys_fields_year_genre_and_midi(catalog, filters, titles):
    store, searcher = catalog
    store.import_songs([
        reference(), reference(title="Open Water", artist="Other Artist", genre="folk",
                               year=2020, lyrics="[Verse]\nThe river carries me away"),
    ])
    assert {s.title for s in searcher.search(**filters)} == titles


def test_import_precedes_bundled_duplicate_and_negative_ids_are_authoritative(catalog):
    store, searcher = catalog
    pq.write_table(pa.Table.from_pylist([{
        "song_id": 7, "title": "City Windows", "artist": "Example Artist",
        "genre": "pop", "tag": "pop", "year": 2020, "lyrics": LYRICS,
        "line_count": 4, "word_count": 32, "section_count": 2,
        "tempo_bpm": 100.0, "musical_key": "C", "scale": "major", "abc_score": "",
        "time_signature": "4/4", "chords": ["Cm"], "album": "", "has_bridge": False,
        "duration_sec": None, "danceability": None, "energy": None, "valence": None,
        "acousticness": None, "loudness": None, "speechiness": None, "roman_numerals": [],
        "vocal_range_low": None, "vocal_range_high": None, "pitch_range_semitones": None,
        "melodic_contour": "", "instruments": [], "tags": [],
    }]), searcher.parquet_glob)
    assert searcher.get_song(song_id=7).tempo_bpm == 100
    song_id = store.import_songs([reference()])["song_ids"][0]
    results = searcher.search("City Windows")
    assert [(s.song_id, s.genre) for s in results] == [(song_id, "darkwave")]
    assert searcher.get_song(song_id=-999, title="City Windows", artist="Example Artist") is None


@pytest.fixture
def client_catalog(catalog, tmp_path, monkeypatch):
    store, searcher = catalog
    api = importlib.import_module("songwriter.web.app")
    wb = Workbench(db_path=tmp_path / "absent.duckdb")
    wb.searcher = searcher
    monkeypatch.setattr(api, "wb", wb)
    monkeypatch.setattr(api.cowriter, "_wb", wb)
    with TestClient(api.app) as client:
        yield client, store, searcher


def test_blueprint_preserves_unspecified_music_metadata(client_catalog):
    client, store, _ = client_catalog
    song_id = store.import_songs([reference(tempo_bpm=None, musical_key=None, scale=None)])["song_ids"][0]
    response = client.get("/api/ai/blueprint", params={"song_id": song_id})
    assert response.status_code == 200
    ref = response.json()["reference_song"]
    assert [ref[field] for field in ("tempo_bpm", "musical_key", "scale")] == [None, None, None]
    assert [section["kind"] for section in ref["sections"]] == ["verse", "chorus"]
    assert ref["chords"] == [] and ref["abc_score"] == ""


@pytest.mark.parametrize("invalid_lyrics", [" ", "[Verse]"])
def test_invalid_batch_saves_nothing_and_signed_http_ids_resolve(client_catalog, invalid_lyrics):
    client, store, searcher = client_catalog
    rejected = client.post("/api/references/import", json={"songs": [
        reference().model_dump(), {**reference().model_dump(), "title": "Broken", "lyrics": invalid_lyrics},
    ]})
    assert rejected.status_code == 422
    assert store.count() == 0
    song_id = store.import_songs([reference()])["song_ids"][0]
    response = client.get(f"/api/songs/{song_id}")
    assert response.status_code == 200
    assert response.json()["raw_lyrics"] == LYRICS
    assert response.json()["sections"][1]["kind"] == "chorus"
    assert client.get("/api/songs/-999").status_code == 404


def test_recommendations_use_supplied_tempo_without_corpus_or_invented_midi(catalog, tmp_path):
    store, searcher = catalog
    result = store.import_songs([
        reference(), reference(title="Unknown Tempo", tempo_bpm=None),
        reference(title="Too Fast", tempo_bpm=170),
    ])
    wb = Workbench(db_path=tmp_path / "absent.duckdb")
    wb.searcher = searcher
    writer = SongwriterCowriter()
    writer._wb = wb
    writer.studio_db.db_path = tmp_path / "absent.duckdb"
    recs = writer.recommend_reference_songs(genre="Darkwave", bpm=125, key="F#", scale="minor", limit=20)
    imported = [r for r in recs if r["song_id"] in result["song_ids"]]
    assert [(r["song_id"], r["bpm"], r["has_midi"]) for r in imported] == [(result["song_ids"][0], 125, False)]
    midi = writer.recommend_reference_songs(genre="Darkwave", bpm=125, has_midi=True, limit=20)
    assert not any(r["song_id"] in result["song_ids"] for r in midi)


def test_automated_refresh_preserves_known_metadata_and_fills_unknowns(catalog):
    store, searcher = catalog
    first = store.import_songs([
        reference(),
        reference(title="Unrated", genre="misc", year=0, tempo_bpm=None, musical_key=None, scale=None),
    ])
    changed = LYRICS.replace("lantern", "candle")
    result = store.import_songs([
        reference(lyrics=changed, genre="pop", year=2014, tempo_bpm=None, musical_key=None, scale=None),
        reference(title="Unrated", genre="pop", year=2014, tempo_bpm=None, musical_key=None, scale=None),
    ], preserve_metadata=True)
    assert result["song_ids"] == first["song_ids"]
    known = searcher.get_song(song_id=first["song_ids"][0])
    assert known.raw_lyrics == changed
    assert (known.genre, known.year, known.tempo_bpm, known.musical_key, known.scale) == ("darkwave", 2023, 125, "F#", "minor")
    unrated = searcher.get_song(song_id=first["song_ids"][1])
    assert (unrated.genre, unrated.year, unrated.tempo_bpm, unrated.musical_key) == ("pop", 2014, None, None)


def test_artist_job_is_exclusive_and_imports_beyond_manual_batch_limit(catalog, monkeypatch):
    store, searcher = catalog
    entered, resume = threading.Event(), threading.Event()
    records = [reference(title=f"Track {i}") for i in range(201)]

    def load(artist, timeout_seconds, progress):
        entered.set()
        if not resume.wait(5):
            raise RuntimeError("Test did not release its import worker")
        return records

    monkeypatch.setattr("songwriter.corpus.artist_imports.CORPUS_DB", store.db.path.parent / "missing.duckdb")
    monkeypatch.setattr("songwriter.corpus.artist_imports.load_artist", load)
    manager = ArtistImportManager()
    manager.start("Example Artist", store)
    try:
        assert entered.wait(5)
        assert manager.status()["status"] == "running"
        with pytest.raises(ImportInProgress):
            manager.start("Another Artist", store)
        assert store.count() == 0
    finally:
        resume.set()
        manager._worker.join(5)
    status = manager.status()
    assert status["status"] == "complete"
    assert (status["found"], status["added"], status["updated"]) == (201, 201, 0)
    assert store.count() == 201
    assert searcher.get_song(title="Track 200", artist="Example Artist").raw_lyrics == LYRICS


def test_artist_job_reports_failed_save_without_partial_import(catalog, monkeypatch):
    store, _ = catalog
    monkeypatch.setattr("songwriter.corpus.artist_imports.load_artist", lambda *args: [
        reference(), reference(title="Broken", lyrics="[Verse]"),
    ])
    monkeypatch.setattr("songwriter.corpus.artist_imports.CORPUS_DB", store.db.path.parent / "missing.duckdb")
    manager = ArtistImportManager()
    manager.start("Example Artist", store)
    manager._worker.join(5)
    status = manager.status()
    assert status["status"] == "failed"
    assert "section headings" in status["error"]
    assert status["song_ids"] == []
    assert store.count() == 0


@pytest.mark.parametrize("has_override", [False, True])
def test_artist_import_reuses_bundled_tracks_unless_an_import_overrides_them(catalog, monkeypatch, has_override):
    store, searcher = catalog
    bundled_path = store.db.path.parent / "bundled.duckdb"
    with duckdb.connect(str(bundled_path)) as conn:
        conn.execute("CREATE TABLE songs(song_id BIGINT, title VARCHAR, artist VARCHAR)")
        conn.execute("INSERT INTO songs VALUES (13, 'City Windows', 'Example Artist')")
    monkeypatch.setattr("songwriter.corpus.artist_imports.CORPUS_DB", bundled_path)
    original_id = store.import_songs([reference()])["song_ids"][0] if has_override else None
    changed = LYRICS.replace("lantern", "candle")
    monkeypatch.setattr("songwriter.corpus.artist_imports.load_artist", lambda *args: [
        reference(lyrics=changed, tempo_bpm=None, musical_key=None, scale=None),
    ])
    manager = ArtistImportManager()
    manager.start("Example Artist", store)
    manager._worker.join(5)
    status = manager.status()
    assert status["status"] == "complete"
    assert status["song_ids"] == ([original_id] if has_override else [13])
    assert status["existing"] == (0 if has_override else 1)
    assert store.count() == (1 if has_override else 0)
    if has_override:
        song = searcher.get_song(song_id=original_id)
        assert song.tempo_bpm == 125 and song.raw_lyrics == changed


def test_parallel_reference_searches_finish_without_a_bundled_corpus(tmp_path):
    # Bound a deadlock regression in a child so it cannot freeze the test suite.
    script = textwrap.dedent("""
        import os
        import sys
        from concurrent.futures import ThreadPoolExecutor
        from pathlib import Path
        from threading import Barrier

        os.environ["SONGWRITER_DATA_DIR"] = sys.argv[1]
        os.environ["SONGWRITER_DUCKDB_THREADS"] = "1"
        from songwriter.corpus.references import ReferenceSongInput, ReferenceStore
        from songwriter.corpus.search import SongSearcher
        from songwriter.db import Database

        root = Path(sys.argv[1])
        store = ReferenceStore(Database.get(root / "workbench.duckdb", read_only=False))
        song_id = store.import_songs([ReferenceSongInput(
            title="City Windows", artist="Example Artist", lyrics="[Verse]\\nThe city lights are fading",
        )])["song_ids"][0]
        searcher = SongSearcher(root / "songs.parquet", root / "absent.duckdb", store)
        ready = Barrier(4)

        def search(_):
            ready.wait()
            songs = searcher.search("City Windows")
            assert [(song.song_id, song.title) for song in songs] == [(song_id, "City Windows")]
            assert searcher.get_song(song_id=song_id).raw_lyrics == "[Verse]\\nThe city lights are fading"

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(search, range(4)))
    """)
    subprocess.run([sys.executable, "-c", script, str(tmp_path)], check=True, timeout=30)
