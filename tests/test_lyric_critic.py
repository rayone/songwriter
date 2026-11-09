"""Step 4 Tier 2 critic: nonsense lines fail the turn, the critic's fixes drive the
retry, and a failed final retry never replaces a stronger earlier take.

Run: uv run pytest tests/test_lyric_critic.py
The corpus checks skip unless app.duckdb is in the data directory.
"""
from __future__ import annotations

import json

import pytest

from songwriter import prompt_templates
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
        assert not any(k in c for k in ("sense", "story", "vernacular", "cliches"))


def test_prompts_edited_in_settings_reach_the_writer_and_the_critic(
    cowriter: SongwriterCowriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    rules = prompt_templates.get_template("line_rules") + "\n- Every line mentions the moon."
    critic = prompt_templates.get_template("lyric_critic").replace(
        "EVALUATION CRITERIA:", "EVALUATION CRITERIA:\n0. Reject any line about the sun.")
    sent: list[str] = []

    def chat(messages: list[dict[str, str]], **kw: object) -> str:
        sent.append(messages[0]["content"])
        if _is_critic(messages):
            return json.dumps({"passed": True, "summary": "Clean.", "lines": []})
        return "Line 1: Take my heart\nLine 2: Break me apart"

    monkeypatch.setattr(cowriter, "_chat", chat)
    prompt_templates.save_version("line_rules", rules)
    prompt_templates.save_version("lyric_critic", critic)
    try:
        list(cowriter.stream_song_interactive(max_retries=1, **_LOVE))
    finally:
        prompt_templates.reset_default("line_rules")
        prompt_templates.reset_default("lyric_critic")

    writer = [p for p in sent if "EVALUATION CRITERIA:" not in p]
    judged = [p for p in sent if "EVALUATION CRITERIA:" in p]
    assert writer and all("Every line mentions the moon." in p for p in writer)
    assert judged and all("Reject any line about the sun." in p for p in judged)


def test_critic_prompt_shares_a_static_prefix_across_turns(cowriter: SongwriterCowriter) -> None:
    # For prompt caching to help, every critic call in a song must share a long
    # identical opening: the static criteria and JSON schema come before any
    # per-turn content (TRACK CONTEXT and the draft lines).
    g = SongGeneration(title="Night Drive", genre="Pop", key="C", scale="major", bpm=100,
                       brief="a drive at dusk", story="x")
    p1 = cowriter._build_critique_prompt(["Take my heart", "Break me apart"], g,
                                         section="Verse 1", line_numbers=[1, 2])
    p2 = cowriter._build_critique_prompt(["City lights blur by", "I let the night decide"], g,
                                         section="Verse 2", line_numbers=[3, 4])
    pre1, pre2 = p1.split("TRACK CONTEXT:")[0], p2.split("TRACK CONTEXT:")[0]
    assert pre1 == pre2 and len(pre1) > 1000
    assert "EVALUATION CRITERIA:" in pre1  # the routing marker is in the shared prefix


def test_critic_runs_on_the_configured_critic_model(
    cowriter: SongwriterCowriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A separate critic model is used for the critic call (not the writer's), and
    # recorded on the critique so the UI can label the judge.
    from songwriter import settings
    monkeypatch.setattr(settings, "get_str",
                        lambda k: "critic-xl" if k == "critic_model" else (settings._raw(k) or ""))
    monkeypatch.setattr(settings, "get", lambda k: settings._SPEC[k][1])
    seen: list[str] = []

    def chat(messages, **kw):
        seen.append(kw.get("model"))
        return json.dumps({"passed": True, "summary": "ok", "lines": []})

    monkeypatch.setattr(cowriter, "_chat", chat)
    c = cowriter._run_llm_critique(["Take my heart", "Break me apart"],
                                   SongGeneration(model="writer-m"))
    assert seen == ["critic-xl"]
    assert c["critic_model"] == "critic-xl"


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


def test_auto_llm_engine_fails_a_contracted_rhyme_that_does_not_land(
    cowriter: SongwriterCowriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    # In Auto LLM mode the engine owns rhyme: a line that does not rhyme with its
    # partner fails the turn even if the critic passes it, so the model never has
    # to judge sound. Needs the corpus for phonetics.
    if cowriter.workbench.db is None:
        pytest.skip("corpus database not available")
    drafts = iter([
        "Line 1: Take my heart\nLine 2: Walking down the street",  # 'street' does not rhyme with 'heart'
        "Line 1: Take my heart\nLine 2: Break me apart",
    ])
    monkeypatch.setattr(cowriter, "_chat",
                        lambda messages, **kw: '{"passed": true, "summary": "ok", "lines": []}'
                        if _is_critic(messages) else next(drafts))
    events = list(cowriter.stream_song_interactive(max_retries=2, **_LOVE))

    first = next(e["validation"] for e in events if e.get("type") == "validation")
    assert first["passed"] is False
    assert any("rhyme with Line 1" in i for i in first["issues"])
    assert [e["line"] for e in events if e.get("type") == "line_accepted"] == ["Take my heart", "Break me apart"]


def test_syllable_miss_beyond_tolerance_fails_smaller_miss_is_advisory(
    cowriter: SongwriterCowriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Guide is 3-6 syllables/line; tolerance 2 means 1-8 passes, outside fails.
    # "Take my heart" is 3 (in guide). A 10-syllable line fails the turn; a line
    # one past the guide is only an advisory, not a failure.
    from songwriter import settings
    monkeypatch.setattr(settings, "get", lambda k: 2 if k == "syllable_tolerance" else settings._raw(k) or settings._SPEC[k][1])
    over = "Line 1: Take my heart\nLine 2: I am running very far away today now"  # ~11 syl, well over 8
    fixed = "Line 1: Take my heart\nLine 2: Break me apart"
    drafts = iter([over, fixed])
    monkeypatch.setattr(cowriter, "_chat",
                        lambda messages, **kw: '{"passed": true, "summary": "ok", "lines": []}'
                        if _is_critic(messages) else next(drafts))
    events = list(cowriter.stream_song_interactive(max_retries=2, **_LOVE))
    first = next(e["validation"] for e in events if e.get("type") == "validation")
    assert first["passed"] is False
    assert any("syllables, aiming" in i for i in first["issues"])
    assert [e["line"] for e in events if e.get("type") == "line_accepted"] == ["Take my heart", "Break me apart"]


def test_every_attempt_nonrhyming_finalizes_incomplete_not_verified(
    cowriter: SongwriterCowriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The writer returns a non-rhyming pair on every attempt. The engine fails them
    # all; the best take is kept so the section is not lost, but the song finalizes
    # as "incomplete" (not verified) with the section flagged, so a persistently
    # non-rhyming draft never ships as finished. Needs the corpus for phonetics.
    if cowriter.workbench.db is None:
        pytest.skip("corpus database not available")
    saved: list[object] = []
    monkeypatch.setattr(cowriter.song_store, "save", lambda gen: saved.append(gen) or gen)
    monkeypatch.setattr(cowriter, "_chat",
                        lambda messages, **kw: '{"passed": true, "summary": "ok", "lines": []}'
                        if _is_critic(messages)
                        else "Line 1: Take my heart\nLine 2: Walking down the street")  # never rhymes 'heart'
    events = list(cowriter.stream_song_interactive(max_retries=2, **_LOVE))

    # Every attempt failed on the engine's rhyme check.
    assert all(e["validation"]["passed"] is False
               for e in events if e.get("type") == "validation")
    inc = [e for e in events if e.get("type") == "section_incomplete"]
    assert inc and inc[0]["reason"] == "did not pass validation"
    done = next(e for e in events if e.get("type") == "complete")
    assert done["status"] == "incomplete"
    assert saved and saved[-1].status == "incomplete"


def test_a_failed_block_makes_the_whole_section_incomplete(
    cowriter: SongwriterCowriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A 4-line ABAB section is two blocks: lines 1-2, then 3-4. The first block
    # passes; the second fails its rhyme on every retry and is kept as the best
    # failed take. The section must still finalize incomplete -- a later passing
    # block must not erase the earlier failure. Needs the corpus for phonetics.
    if cowriter.workbench.db is None:
        pytest.skip("corpus database not available")
    four = dict(
        prompt="love", story="x", model="test-model", section_plan="[Verse 1]",
        syllable_target="3-6", rhyme_scheme="ABAB", max_sections=1,
        answers={"section_lines": "4 lines per section", "lines_per_turn": "2 lines per turn"},
    )
    # Block 1 (lines 1-2, A/B openers, no rhyme contract between them) passes.
    # Block 2 (lines 3-4): line 3 must rhyme line 1 ("heart"), line 4 line 2
    # ("street"); these never do, so every retry fails.
    def chat(messages, **kw):
        if _is_critic(messages):
            return '{"passed": true, "summary": "ok", "lines": []}'
        c = messages[0]["content"]
        if "Lines 3-4" in c or "Line 3" in c:
            return "Line 3: I wander far away\nLine 4: beneath the open sky"  # no rhyme with heart/street
        return "Line 1: Take my heart\nLine 2: Walking down the street"

    saved: list[object] = []
    monkeypatch.setattr(cowriter.song_store, "save", lambda gen: saved.append(gen) or gen)
    monkeypatch.setattr(cowriter, "_chat", chat)
    events = list(cowriter.stream_song_interactive(max_retries=2, **four))

    vals = [e["validation"]["passed"] for e in events if e.get("type") == "validation"]
    assert True in vals and False in vals  # block 1 passed, block 2 failed
    done = next(e for e in events if e.get("type") == "complete")
    assert done["status"] == "incomplete"
    assert any(s["reason"] == "did not pass validation" for s in done["incomplete_sections"])
    assert saved and saved[-1].status == "incomplete"
