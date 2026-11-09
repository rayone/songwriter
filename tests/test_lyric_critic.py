"""Step 4 Tier 2 critic: nonsense lines fail the turn, the critic's fixes drive the
retry, and a failed final retry never replaces a stronger earlier take.

Run: uv run pytest tests/test_lyric_critic.py
The corpus checks skip unless app.duckdb is in the data directory.
"""
from __future__ import annotations

import json

import pytest

from songwriter.ai.cowriter import SongwriterCowriter
from songwriter.ai.generation import SongGeneration

# One 2-line AABB verse, so each test drives a single turn.
_LOVE = dict(
    prompt="love", story="Giving everything to a love that keeps hurting me.", model="test-model",
    section_plan="[Verse 1]", syllable_target="3-6", rhyme_scheme="AABB", max_sections=1,
    answers={"section_lines": "2 lines per section"},
)


@pytest.fixture(scope="module")
def cowriter() -> SongwriterCowriter:
    return SongwriterCowriter()


def _is_critic(messages: list[dict[str, str]]) -> bool:
    return "EVALUATION CRITERIA:" in messages[0]["content"]


def test_nonsense_line_fails_the_turn_and_its_fix_drives_the_retry(
    cowriter: SongwriterCowriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    drafts = iter([
        "Line 1: Take my heart\nLine 2: Tear me up apart",
        "Line 1: Take my heart\nLine 2: Break me apart",
    ])
    critiques = iter([
        # The model's own flag says pass; its line-level read says line 2 is nonsense.
        json.dumps({"passed": True, "summary": "Line 2 splices two idioms.",
                    "lines": [{"line": 1, "makes_sense": True, "fix": ""},
                              {"line": 2, "meaning": "rip me to pieces", "makes_sense": False,
                               "forced_word": "up", "fix": "Break me apart"}]}),
        json.dumps({"passed": True, "summary": "Clean.", "lines": []}),
    ])
    monkeypatch.setattr(cowriter, "_chat",
                        lambda messages, **kw: next(critiques) if _is_critic(messages) else next(drafts))
    events = list(cowriter.stream_song_interactive(max_retries=3, **_LOVE))

    first = next(e["validation"] for e in events if e.get("type") == "validation")
    assert first["passed"] is False
    assert any("Line 2 does not make sense" in i and "'up'" in i for i in first["issues"])
    retry = [e["prompt"] for e in events if e.get("type") == "prompt"][1]
    assert "Line 2: Break me apart" in retry.split("TASK (REVISION)")[1]
    assert [e["line"] for e in events if e.get("type") == "line_accepted"] == ["Take my heart", "Break me apart"]


def test_final_retry_never_replaces_a_stronger_earlier_take(
    cowriter: SongwriterCowriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    drafts = iter([
        "Line 1: Take my heart\nLine 2: Break me apart",  # clears engine checks; critic wants more
        "Line 1: Take my heart",                          # final retry drops a line
    ])
    critique = json.dumps({"passed": False, "summary": "The couplet circles.",
                           "story": {"score": 3, "verdict": "fail", "feedback": "no new beat"}})
    monkeypatch.setattr(cowriter, "_chat",
                        lambda messages, **kw: critique if _is_critic(messages) else next(drafts))
    events = list(cowriter.stream_song_interactive(max_retries=2, **_LOVE))

    assert [e["validation"]["kept_attempt"] for e in events if e.get("type") == "validation"] == [None, 1]
    assert [e["line"] for e in events if e.get("type") == "line_accepted"] == ["Take my heart", "Break me apart"]


@pytest.mark.parametrize("second, parsed", [
    ("Sure! These lines look great.", False),
    ('{"passed": false, "summary": "Line 2 is forced.", "lines": []}', True),
])
def test_unreadable_critic_reply_is_retried_once_and_never_scored_as_a_pass(
    cowriter: SongwriterCowriter, monkeypatch: pytest.MonkeyPatch, second: str, parsed: bool
) -> None:
    replies = iter(["Sure! These lines look great.", second])
    calls = []

    def mock_chat(messages, **kw):
        calls.append(messages)
        return next(replies)
    monkeypatch.setattr(cowriter, "_chat", mock_chat)
    c = cowriter._run_llm_critique(["Take my heart", "Break me apart"], SongGeneration(model="test-model"))
    assert len(calls) == 2
    assert c["parsed"] is parsed
    if parsed:
        assert c["passed"] is False
    else:
        assert c["score"] is None
        assert not any(k in c for k in ("sense", "story", "rhyme_integrity", "vernacular"))


def test_unseen_pairs_flag_spliced_idioms_not_fresh_images(cowriter: SongwriterCowriter) -> None:
    wb = cowriter.workbench
    if wb.db is None:
        pytest.skip("corpus database not available")
    assert "up apart" in wb.unseen_pairs("Tear me up apart (ooh-whoa)")
    # Rare words that never met ('gravel street') are a fresh image, not an error.
    assert wb.unseen_pairs("Cold rain falling on the gravel street") == []


def test_rhyme_landings_respect_the_song_register(cowriter: SongwriterCowriter) -> None:
    wb = cowriter.workbench
    if wb.db is None:
        pytest.skip("corpus database not available")
    raw = wb.rhyme_landings(["purr"], per_word=8).get("purr", [])
    if not any("pussy" in p for p in raw):
        pytest.skip("corpus has no vulgar landing for 'purr' to filter")
    clean = wb.rhyme_landings(["purr"], per_word=8, exclude_tags=("offensive", "vulgar")).get("purr", [])
    assert clean and not any("pussy" in p for p in clean)
