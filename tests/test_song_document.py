import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from songwriter.ai.generation import SongGeneration
from songwriter.ai.song_store import SongStore
from songwriter.db import Database


def test_title_edit_roundtrips_the_complete_song_via_app_api(tmp_path, monkeypatch):
    from songwriter.web import app as web

    store = SongStore(Database(tmp_path / "songs.duckdb", read_only=False))
    monkeypatch.setattr(web.cowriter, "song_store", store)
    client = TestClient(web.app)
    song = SongGeneration(
        title="Before", model="selected-writer", language="French",
        genre="Electropop", subgenre="Dark Pop", bpm=100, key="Bb", scale="major",
        moods=["tense", "intimate"], instruments=["analog synths", "trap drums"],
        time_signature="3/4", vocal="airy female vocal", duration_hint_sec=145,
        brief="A midnight escape", story="She leaves the city behind.",
        questions=[{"id": "ending", "question": "How does it end?", "default": "escape"}],
        answers={"ending": "escape"},
        sections=[{"name": "Verse 1", "kind": "verse", "lines": ["La nuit me suit"],
                   "attempt_history": [{"attempt": 1, "issues": ["too long"]}],
                   "rhyme_api_audit": [{"word": "suit", "matches": ["nuit"]}]}],
        engine_prompts={"yue2": "French, Electropop, analog synths, airy female vocal, 100 BPM",
                        "minimax": "An authored caption, with its exact punctuation.",
                        "songbench": "Dark Pop, trap drums"},
        engine_prompt_author="model", engine_prompt_section_arc="quiet opening, bright release",
        turns=[{"type": "response", "text": "La nuit me suit"}],
        incomplete_sections=[{"name": "Chorus", "have": 0, "planned": 4}],
        diagnostics={"final_issues": ["Chorus incomplete"]},
        analytics={"total_lines": 1}, status="incomplete",
    )
    saved = client.post("/api/crafted_songs", json=song.model_dump(mode="json"))
    assert saved.status_code == 200, saved.text
    original = saved.json()
    edited = dict(original, title="After")
    # A stale derived value cannot become a competing source of truth.
    edited["lyrics"] = "This is not the saved section text"
    updated = client.post("/api/crafted_songs", json=edited)
    assert updated.status_code == 200, updated.text
    current = updated.json()
    assert current["revision"] == original["revision"] + 1
    assert current["lyrics"] == "[Verse 1]\nLa nuit me suit"
    for field in SongGeneration.model_fields:
        if field not in {"title", "revision", "updated_at"}:
            assert current[field] == original[field], field
    latest = client.get(f"/api/crafted_songs/{current['song_id']}")
    assert latest.json() == current
    previous = client.get(f"/api/crafted_songs/{current['song_id']}/revisions/{original['revision']}")
    assert previous.status_code == 200
    assert previous.json()["title"] == "Before"
    assert previous.json()["engine_prompts"] == original["engine_prompts"]
    assert previous.json()["sections"] == original["sections"]
    unchanged = client.post("/api/crafted_songs", json=current)
    assert unchanged.json()["revision"] == current["revision"]


def test_status_only_transition_is_revisioned(tmp_path):
    store = SongStore(Database(tmp_path / "status.duckdb", read_only=False))
    song = SongGeneration(model="writer", status="incomplete")
    store.save(song)
    first_revision = song.revision
    song.status = "finalized"
    store.save(song)
    assert song.revision == first_revision + 1
    assert store.load_revision(song.song_id, first_revision).status == "incomplete"
    assert store.load(song.song_id).status == "finalized"


def test_legacy_revision_does_not_borrow_current_status(tmp_path):
    db = Database(tmp_path / "legacy.duckdb", read_only=False)
    document = json.dumps({"title": "Legacy", "model": "writer", "sections": [],
                           "engine_prompts": {"yue2": "An exact legacy style"}})
    with db.transaction() as con:
        con.execute("""
            CREATE TABLE crafted_songs (
                id VARCHAR PRIMARY KEY, revision INTEGER, status VARCHAR,
                created_at TIMESTAMP DEFAULT current_timestamp,
                updated_at TIMESTAMP DEFAULT current_timestamp,
                rating INTEGER, notes VARCHAR, generation JSON
            );
            CREATE TABLE song_revisions (
                song_id VARCHAR, revision INTEGER,
                created_at TIMESTAMP DEFAULT current_timestamp, generation JSON,
                PRIMARY KEY (song_id, revision)
            );
        """)
        con.execute("INSERT INTO crafted_songs (id, revision, status, generation) VALUES ('legacy', 1, 'rendered', ?)", (document,))
        con.execute("INSERT INTO song_revisions (song_id, revision, generation) VALUES ('legacy', 1, ?)", (document,))
    store = SongStore(db)
    assert store.load("legacy").status == "rendered"
    historical = store.load_revision("legacy", 1)
    assert historical.status is None
    assert historical.language is None
    assert historical.duration_hint_sec is None
    assert historical.engine_prompts == {"yue2": "An exact legacy style"}


def test_unrecognized_song_data_is_rejected_not_silently_discarded():
    with pytest.raises(ValidationError):
        SongGeneration.from_record({"title": "Song", "unrecognized_song_parameter": "keep me"})


@pytest.mark.parametrize("follow", [True, False])
def test_resolving_a_complete_song_preserves_explicit_reference_choices(follow):
    song = SongGeneration(
        model="writer", reference_song={"sections": [{"name": "Verse 1", "lines": ["A short line"]}]},
        follow_reference_syllables=follow, follow_reference_lines=follow, follow_reference_rhyme=follow,
        syllable_min=7, syllable_max=9, section_lines=6, rhyme_scheme="ABAB",
    )
    restored = SongGeneration.model_validate(song.model_dump(mode="json"))
    restored.resolve_inputs({})
    assert restored.follow_reference_syllables is follow
    assert restored.follow_reference_lines is follow
    assert restored.follow_reference_rhyme is follow


def test_stock_ban_choice_preserves_custom_banned_pairs():
    song = SongGeneration(
        model="writer", banned_pairs=["blue/you", "night/light"], answers={"banned": "None"},
    )
    song.resolve_inputs({})
    assert song.banned_pairs == ["blue/you"]


def test_human_review_uses_producer_turn_size_and_preserves_generated_analysis(tmp_path, monkeypatch):
    from songwriter.ai.cowriter import SongwriterCowriter

    writer = SongwriterCowriter()
    writer.song_store = SongStore(Database(tmp_path / "human.duckdb", read_only=False))
    song = SongGeneration(
        model="writer", brief="Leaving the city", story="She leaves at dawn.",
        section_plan=["Verse 1"], syllable_min=3, syllable_max=6, rhyme_scheme="free",
        author_engine_prompt=False,
        answers={"section_lines": "4 lines per section", "lines_per_turn": "Whole section"},
    )
    monkeypatch.setattr(
        writer, "_chat",
        lambda *args, **kwargs: "L1: Take my heart\nL2: Break me apart\nL3: Walk beside me\nL4: Stand in the light",
    )
    writer.draft_turn(song)
    proposal = song.turns[-1]
    assert proposal["line_numbers"] == [1, 2, 3, 4]
    assert proposal["scan"][0]["syllables"] == 3
    assert song.lyrics == ""  # Proposed text is not accepted song text.
    writer.finalize_turns(song, accepted=[{"section": "Verse 1", "lines": proposal["lines"]}])
    restored = writer.song_store.load(song.song_id)
    assert restored.lyrics == "[Verse 1]\nTake my heart\nBreak me apart\nWalk beside me\nStand in the light"
    assert restored.turns[-1]["scan"][0]["syllables"] == 3
    assert restored.turns[-1]["line_numbers"] == [1, 2, 3, 4]
