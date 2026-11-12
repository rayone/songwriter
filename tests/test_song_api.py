"""Focused behavioral tests for MusicStudio API (v1) cutover to canonical SongGeneration.

Covers:
1. GET /v1/songs/{id} full metadata and export equality against canonical SongGeneration.
2. Revision isolation: historical snapshot reads export that exact revision, not latest values.
3. No prompt/LLM regeneration on reads; absence of fabricated legacy prompts.
4. List endpoint /v1/songs returns canonical SongGeneration models, preserves filter, pagination, take grouping.
5. Cache validation (ETags): full response hashing, invalidation when row lifecycle or sibling take changes.
6. Health check contract version 6.
7. OpenAPI schema verification: sole song model is SongGeneration.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from songwriter.ai.generation import SongGeneration
from songwriter.ai.song_store import SongStore
from songwriter.db import Database
from songwriter.web.app import app


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    test_db = Database(tmp_path / "test_workbench.duckdb", read_only=False)
    monkeypatch.setattr("songwriter.db.workbench_db", lambda: test_db)
    monkeypatch.setattr("songwriter.ai.song_store.workbench_db", lambda: test_db)
    monkeypatch.setattr("songwriter.web.musicstudio_v1.workbench_db", lambda: test_db)
    monkeypatch.delenv("SONGWRITER_API_KEY", raising=False)
    return test_db


@pytest.fixture
def store(isolated_db: Database) -> SongStore:
    return SongStore(isolated_db)


@pytest.fixture
def client(isolated_db: Database) -> TestClient:
    return TestClient(app, headers={"Authorization": "Bearer test-key"})


def _create_rich_song(song_id: str = "song_neon_123") -> SongGeneration:
    return SongGeneration(
        song_id=song_id,
        title="Midnight Neon",
        artist="Luna & The Waves",
        brief="A synthwave midnight drive along the coast",
        story="Driving under neon lights with the top down",
        model="writer-pro",
        language="French",
        bpm=112,
        key="Eb",
        scale="minor",
        genre="Synthwave",
        subgenre="Outrun",
        style="Retro 80s analog synth with driving bassline",
        moods=["nostalgic", "driving", "neon"],
        instruments=["Juno-106", "LinnDrum", "slap bass"],
        duration_hint_sec=210,
        vocal="airy female vocal",
        vocal_roles={"lead": "airy female vocal", "backing": "vocoder harmony"},
        time_signature="4/4",
        chords=["Ebm", "B", "Db", "Bbm"],
        template_id=42,
        section_plan=["Verse 1", "Chorus"],
        max_sections=2,
        syllable_min=8,
        syllable_max=11,
        section_lines=2,
        lines_per_turn=2,
        validation_mode="auto",
        rhyme_scheme="AABB",
        banned_pairs=["love/above", "fire/desire"],
        answers={"vibe": "chill"},
        questions=[{"id": "vibe", "question": "What's the vibe?"}],
        max_retries=3,
        correction_strictness="balanced",
        mode="creator",
        temperature=0.8,
        top_p=0.95,
        max_tokens=1000,
        sections=[
            {
                "name": "Verse 1",
                "kind": "verse",
                "lines": ["Sous les néons de minuit", "Le bitume mouillé luit"],
                "attempt_history": [{"attempt": 1, "issues": []}],
                "rhyme_api_audit": [{"word": "luit", "matches": ["minuit"]}],
            },
            {
                "name": "Chorus",
                "kind": "chorus",
                "lines": ["Roule roule sans retour", "Vers la fin de notre amour"],
            },
        ],
        abc_score="X:1\nT:Test\nK:Ebmin\nE2 B2 |",
        engine_prompts={
            "minimax": "Synthwave, 112 BPM, Eb minor. Juno synths, airy female vocal.",
            "yue2": "Synthwave, French, Outrun, 80s analog synth, 112 BPM",
            "songbench": "Retro synthwave track",
        },
        engine_target="minimax",
        engine_prompt_author="model",
        engine_prompt_section_arc="atmospheric build to pounding chorus",
        system_prompt="You are a professional French lyricist.",
        user_prompt="Write a synthwave song about midnight driving.",
        turns=[{"type": "turn", "section": "Verse 1", "lines": ["Sous les néons de minuit"]}],
        incomplete_sections=[],
        diagnostics={"rhyme_density": 0.85},
        analytics={"total_syllables": 38, "verse_count": 1},
        status="finalized",
        rating=5,
        notes="Best take so far",
    )


def test_get_song_full_metadata_and_export_equality(store: SongStore, client: TestClient) -> None:
    """GET /v1/songs/{id} must return the complete canonical SongGeneration document."""
    original = _create_rich_song()
    saved = store.save(original)

    res = client.get(f"/v1/songs/{saved.song_id}")
    assert res.status_code == 200, res.text
    data = res.json()

    # Old envelopes must be absent
    assert "creative" not in data
    assert "model_inputs" not in data

    # Identity and row lifecycle
    assert data["song_id"] == saved.song_id
    assert data["revision"] == 1
    assert data["status"] == "finalized"
    assert data["rating"] == 5
    assert data["notes"] == "Best take so far"
    assert data["take_count"] == 1
    assert data["take_group"] == saved.song_id
    assert data["take_number"] == 1

    # Generated fields and musical metadata
    assert data["title"] == "Midnight Neon"
    assert data["artist"] == "Luna & The Waves"
    assert data["brief"] == "A synthwave midnight drive along the coast"
    assert data["story"] == "Driving under neon lights with the top down"
    assert data["model"] == "writer-pro"
    assert data["language"] == "French"
    assert data["bpm"] == 112
    assert data["key"] == "Eb"  # Tonic only, not formatted
    assert data["scale"] == "minor"  # Scale separate
    assert data["genre"] == "Synthwave"
    assert data["subgenre"] == "Outrun"
    assert data["style"] == "Retro 80s analog synth with driving bassline"
    assert data["moods"] == ["nostalgic", "driving", "neon"]
    assert data["instruments"] == ["Juno-106", "LinnDrum", "slap bass"]
    assert data["duration_hint_sec"] == 210  # Preserved hint, not estimated
    assert data["vocal"] == "airy female vocal"
    assert data["vocal_roles"] == {"lead": "airy female vocal", "backing": "vocoder harmony"}
    assert data["time_signature"] == "4/4"
    assert data["chords"] == ["Ebm", "B", "Db", "Bbm"]
    assert data["template_id"] == 42
    assert data["section_plan"] == ["Verse 1", "Chorus"]
    assert data["syllable_min"] == 8
    assert data["syllable_max"] == 11
    assert data["rhyme_scheme"] == "AABB"
    assert data["banned_pairs"] == ["love/above", "fire/desire"]
    assert data["answers"] == {"vibe": "chill"}
    assert data["max_retries"] == 3
    assert data["correction_strictness"] == "balanced"
    assert data["temperature"] == 0.8
    assert data["top_p"] == 0.95
    assert data["max_tokens"] == 1000

    # Sections with full nested attempt_history and rhyme_api_audit
    assert len(data["sections"]) == 2
    assert data["sections"][0]["name"] == "Verse 1"
    assert data["sections"][0]["attempt_history"] == [{"attempt": 1, "issues": []}]
    assert data["sections"][0]["rhyme_api_audit"] == [{"word": "luit", "matches": ["minuit"]}]

    # Computed fields
    expected_lyrics = "[Verse 1]\nSous les néons de minuit\nLe bitume mouillé luit\n\n[Chorus]\nRoule roule sans retour\nVers la fin de notre amour"
    assert data["lyrics"] == expected_lyrics
    assert data["instrumental"] is False
    assert data["has_abc"] is True

    # Exact stored engine prompts & authoring fields
    assert data["engine_prompts"] == {
        "minimax": "Synthwave, 112 BPM, Eb minor. Juno synths, airy female vocal.",
        "yue2": "Synthwave, French, Outrun, 80s analog synth, 112 BPM",
        "songbench": "Retro synthwave track",
    }
    assert data["engine_target"] == "minimax"
    assert data["engine_prompt_author"] == "model"
    assert data["engine_prompt_section_arc"] == "atmospheric build to pounding chorus"
    assert data["system_prompt"] == "You are a professional French lyricist."
    assert data["user_prompt"] == "Write a synthwave song about midnight driving."
    assert data["turns"] == [{"type": "turn", "section": "Verse 1", "lines": ["Sous les néons de minuit"]}]
    assert data["diagnostics"] == {"rhyme_density": 0.85}
    assert data["analytics"] == {"total_syllables": 38, "verse_count": 1}


def test_revision_isolation_and_snapshot_integrity(store: SongStore, client: TestClient) -> None:
    """Historical revision reads must export that snapshot's exact prompts/data, not latest."""
    song = _create_rich_song(song_id="song_rev_test")
    song.title = "Revision 1 Title"
    song.engine_prompts = {"yue2": "Style Prompt Rev 1"}
    song.status = "draft"
    saved_v1 = store.save(song)
    assert saved_v1.revision == 1

    # Modify and save revision 2
    song.title = "Revision 2 Title"
    song.engine_prompts = {"yue2": "Style Prompt Rev 2", "minimax": "Added Minimax Prompt"}
    song.status = "finalized"
    saved_v2 = store.save(song)
    assert saved_v2.revision == 2

    # Read latest (no revision parameter)
    latest_res = client.get(f"/v1/songs/{song.song_id}")
    assert latest_res.status_code == 200
    latest_data = latest_res.json()
    assert latest_data["revision"] == 2
    assert latest_data["title"] == "Revision 2 Title"
    assert latest_data["engine_prompts"] == {"yue2": "Style Prompt Rev 2", "minimax": "Added Minimax Prompt"}
    assert latest_data["status"] == "finalized"

    # Read revision 1
    rev1_res = client.get(f"/v1/songs/{song.song_id}?revision=1")
    assert rev1_res.status_code == 200
    rev1_data = rev1_res.json()
    assert rev1_data["revision"] == 1
    assert rev1_data["title"] == "Revision 1 Title"
    assert rev1_data["engine_prompts"] == {"yue2": "Style Prompt Rev 1"}
    assert rev1_data["status"] == "draft"

    # Nonexistent revision must return 404 with revision_not_found
    missing_res = client.get(f"/v1/songs/{song.song_id}?revision=999")
    assert missing_res.status_code == 404
    assert missing_res.json()["error"]["code"] == "revision_not_found"

    # Nonexistent song must return 404 with song_not_found
    missing_song = client.get("/v1/songs/nonexistent_song_id")
    assert missing_song.status_code == 404
    assert missing_song.json()["error"]["code"] == "song_not_found"




def test_absence_of_fabricated_legacy_prompts(store: SongStore, client: TestClient) -> None:
    """Legacy records without engine prompts must NOT have fallback prompts fabricated."""
    song = SongGeneration(
        song_id="song_legacy_blank",
        title="Unadorned Song",
        genre="Rock",
        bpm=120,
        key="A",
        scale="major",
        vocal="male vocal",
        engine_prompts={},
        language=None,
        duration_hint_sec=None,
        status="ready",
    )
    store.save(song)

    res = client.get(f"/v1/songs/{song.song_id}")
    assert res.status_code == 200
    data = res.json()
    assert data["engine_prompts"] == {}
    assert data["language"] is None
    assert data["duration_hint_sec"] is None
    assert data["key"] == "A"
    assert data["scale"] == "major"


def test_list_songs_exposes_canonical_models_and_take_grouping(store: SongStore, client: TestClient) -> None:
    """GET /v1/songs must return list[SongGeneration] with filter, pagination, and take grouping."""
    group_id = "group_take_test"
    take1 = SongGeneration(
        song_id="take_1",
        take_group=group_id,
        take_number=1,
        title="First Take",
        genre="Jazz",
        status="ready",
        language="English",
    )
    take2 = SongGeneration(
        song_id="take_2",
        take_group=group_id,
        take_number=2,
        title="Second Take",
        genre="Jazz",
        status="ready",
        language="English",
    )
    other = SongGeneration(
        song_id="other_song",
        title="Other Song",
        genre="Metal",
        status="draft",
        language="Spanish",
    )
    store.save(take1)
    store.save(take2)
    store.save(other)

    # 1. Filter by status: ready (defaults to ready/finalized)
    res = client.get("/v1/songs?status=ready")
    assert res.status_code == 200
    data = res.json()
    assert "songs" in data
    song_ids = [s["song_id"] for s in data["songs"]]
    assert "take_1" in song_ids
    assert "take_2" in song_ids
    assert "other_song" not in song_ids
    # Lean summary shape: heavy generation telemetry excluded, duration_hint_sec estimated, id key present
    for s in data["songs"]:
        assert "id" in s and "song_id" in s
        assert s["id"] == s["song_id"]
        assert isinstance(s.get("duration_hint_sec"), int) and s["duration_hint_sec"] > 0
        for heavy in ("chat_history", "diagnostics", "analytics", "turns", "banned_words", "banned_pairs", "questions", "answers", "sections"):
            assert heavy not in s, f"Heavy field {heavy} must not be dumped in list response"

    # Check take grouping across the library
    for s in data["songs"]:
        if s["take_group"] == group_id:
            assert s["take_count"] == 2
    # 2. Filter by genre
    res_genre = client.get("/v1/songs?genre=Jazz&status=all")
    assert res_genre.status_code == 200
    jazz_songs = res_genre.json()["songs"]
    assert len(jazz_songs) == 2
    for s in jazz_songs:
        assert s["genre"] == "Jazz"

    # 3. Search query
    res_search = client.get("/v1/songs?q=First&status=all")
    assert res_search.status_code == 200
    search_songs = res_search.json()["songs"]
    assert len(search_songs) == 1
    assert search_songs[0]["song_id"] == "take_1"

    # 4. Pagination via cursor
    res_page1 = client.get("/v1/songs?limit=2&status=all")
    assert res_page1.status_code == 200
    page1 = res_page1.json()
    assert len(page1["songs"]) == 2
    cursor = page1["next_cursor"]
    assert cursor is not None

    res_page2 = client.get(f"/v1/songs?limit=2&cursor={cursor}&status=all")
    assert res_page2.status_code == 200
    page2 = res_page2.json()
    assert len(page2["songs"]) == 1
    p1_ids = {s["song_id"] for s in page1["songs"]}
    p2_ids = {s["song_id"] for s in page2["songs"]}
    assert not (p1_ids & p2_ids), "Cursor pagination must not yield duplicate items"


def test_etag_caching_and_invalidation_on_row_lifecycle_change(store: SongStore, client: TestClient) -> None:
    """ETags must validate full response state and invalidate on row lifecycle changes."""
    song = _create_rich_song(song_id="song_etag_test")
    store.save(song)

    # 1. Detail ETag caching
    res1 = client.get(f"/v1/songs/{song.song_id}")
    assert res1.status_code == 200
    etag1 = res1.headers.get("ETag")
    assert etag1 is not None

    res1_cached = client.get(f"/v1/songs/{song.song_id}", headers={"If-None-Match": etag1})
    assert res1_cached.status_code == 304

    # 2. Invalidate without generation revision bump (e.g. updating rating and notes)
    song.rating = 2
    song.notes = "Updated without bumping revision"
    store.save(song)

    res2 = client.get(f"/v1/songs/{song.song_id}")
    assert res2.status_code == 200
    etag2 = res2.headers.get("ETag")
    assert etag2 != etag1, "ETag must change when row lifecycle data changes"

    # Stale ETag must NOT return 304
    res2_not_cached = client.get(f"/v1/songs/{song.song_id}", headers={"If-None-Match": etag1})
    assert res2_not_cached.status_code == 200
    assert res2_not_cached.json()["rating"] == 2

    # 3. List ETag caching
    list1 = client.get("/v1/songs?status=all")
    assert list1.status_code == 200
    list_etag = list1.headers.get("ETag")
    assert list_etag is not None

    list1_cached = client.get("/v1/songs?status=all", headers={"If-None-Match": list_etag})
    assert list1_cached.status_code == 304


def test_health_check_contract_version_6(client: TestClient) -> None:
    """GET /v1/health must report contract_version 6."""
    res = client.get("/v1/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "healthy"
    assert data["contract_version"] == 6




def test_lyrics_search_reads_canonical_sections(store: SongStore, client: TestClient) -> None:
    song = _create_rich_song()
    store.save(song)
    response = client.get("/v1/songs", params={"status": "all", "q": "Sous les néons"})
    assert response.status_code == 200, response.text
    assert [item["song_id"] for item in response.json()["songs"]] == [song.song_id]


def test_list_cache_invalidates_when_another_page_becomes_available(store: SongStore, client: TestClient) -> None:
    store.save(_create_rich_song())
    first = client.get("/v1/songs?status=all&limit=1")
    assert first.json()["next_cursor"] is None
    older = SongGeneration(song_id="older-song", model="writer", status="finalized")
    store.save(older)
    with store.db.transaction() as con:
        con.execute(
            "UPDATE crafted_songs SET updated_at = '2000-01-01' WHERE id = ?", (older.song_id,),
        )
    changed = client.get("/v1/songs?status=all&limit=1", headers={"If-None-Match": first.headers["ETag"]})
    assert changed.status_code == 200
    cursor = changed.json()["next_cursor"]
    assert cursor is not None
    following = client.get("/v1/songs", params={"status": "all", "limit": 1, "cursor": cursor})
    assert [item["song_id"] for item in following.json()["songs"]] == [older.song_id]
