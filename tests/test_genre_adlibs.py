"""Step 3: the genre decides whether a vocal end tag (", hey") is allowed. The
reference only selects which tags, never forces one onto a genre that forbids it.

Pop/rap/trap allow end tags even with no reference; electronic/ambient never do,
even when the reference is full of them. A forbidden tag is stripped before
validation, so it can never reach the final lyrics via any attempt.

Run: uv run pytest tests/test_genre_adlibs.py
"""
from __future__ import annotations

import pytest

from songwriter.ai.cowriter import SongwriterCowriter
from songwriter.ai.generation import SongGeneration

C = SongwriterCowriter


def _gen(genre: str, ref_tags: bool = False) -> SongGeneration:
    ref = None
    if ref_tags:
        # A reference whose lines mostly end on ", hey" / ", yeah": delivery_tags()
        # will report them as the reference's delivery.
        lines = [{"text": f"something happens here, {t}"} for t in ("hey", "yeah", "hey", "yeah", "hey")]
        ref = {"title": "Ref", "artist": "A", "genre": "pop", "sections": [{"lines": lines}]}
    return SongGeneration(genre=genre, reference_song=ref)


def test_pop_allows_end_tags_without_a_reference() -> None:
    assert C._genre_allows_end_tags(_gen("Pop")) is True
    assert C._genre_allows_end_tags(_gen("Trap")) is True
    assert C._genre_allows_end_tags(_gen("Hip-Hop")) is True


def test_electronic_and_unknown_genres_forbid_end_tags() -> None:
    assert C._genre_allows_end_tags(_gen("Ethereal Trance Ambient")) is False
    assert C._genre_allows_end_tags(_gen("Dark Synthwave")) is False
    assert C._genre_allows_end_tags(_gen("Progressive House")) is False
    # An unrecognised genre also adds no tags rather than defaulting to "(hey)".
    assert C._genre_allows_end_tags(_gen("Gregorian Zither Core")) is False



def test_settings_no_end_tag_genres_list_drives_the_keyword_ban(monkeypatch: "pytest.MonkeyPatch") -> None:
    # Adding a keyword to the Settings list forbids tags for that genre; a genre
    # not on the list and not an electronic set keeps them. Blank is NOT tested as
    # "re-allow": blank resets to the default keyword list via set_many, and the
    # built-in electronic-set ban stands regardless of the keyword list.
    from songwriter import settings
    monkeypatch.setattr(settings, "get_str", lambda k: "polka" if k == "no_end_tag_genres" else "")
    assert C._genre_allows_end_tags(_gen("Polka Party")) is False   # banned by keyword
    assert C._genre_allows_end_tags(_gen("Pop")) is True            # not on the list
    assert C._genre_allows_end_tags(_gen("Ambient Drift")) is False  # electronic set ban still holds


def test_reference_tags_are_offered_for_pop_but_not_for_trance() -> None:
    # The reference is full of ", hey"/", yeah"; pop takes them, trance ignores them.
    pop = _gen("Pop", ref_tags=True)
    assert pop.delivery_tags(), "reference should register delivery tags"
    assert C._end_tags_for(pop), "pop should be offered the reference's tags"
    assert C._end_tags_for(_gen("Ethereal Trance Ambient", ref_tags=True)) == []


def test_forbidden_end_tag_is_stripped_from_a_line() -> None:
    # The decoration is removed; the real words stay.
    assert C._strip_end_tag("Shadows drift across the water, hey") == "Shadows drift across the water"
    assert C._strip_end_tag("Lost in the echo (yeah)") == "Lost in the echo"
    assert C._strip_end_tag("No tag here") == "No tag here"


def test_every_ambient_reply_with_hey_is_stripped_before_it_ships(monkeypatch: pytest.MonkeyPatch) -> None:
    # Even if the writer ends every attempt on ", hey", a trance song must never
    # ship the tag: it is stripped before validation, so the kept lines are clean.
    # (Whether the turn passes depends on the corpus rhyme check; this test asserts
    # only the stripping, which holds with or without the corpus.)
    cw = SongwriterCowriter()
    love = dict(
        prompt="night drive", story="x", model="test-model", genre="Ethereal Trance Ambient",
        section_plan="[Verse 1]", syllable_target="3-6", rhyme_scheme="AABB", max_sections=1,
        answers={"section_lines": "2 lines per section", "validation_mode": "auto songwriter"},
    )

    def chat(messages, **kw):
        if "EVALUATION CRITERIA:" in messages[0]["content"]:
            return '{"passed": true, "summary": "ok", "lines": []}'
        return "Line 1: Take my heart, hey\nLine 2: Break me apart, hey"

    monkeypatch.setattr(cw, "_chat", chat)
    events = list(cw.stream_song_interactive(max_retries=2, **love))
    # Every response the engine saw had the tag stripped.
    for e in events:
        if e.get("type") == "response":
            assert "hey" not in e["response"].lower()
    accepted = [e["line"] for e in events if e.get("type") == "line_accepted"]
    assert accepted == ["Take my heart", "Break me apart"]
    assert not any("hey" in line.lower() for line in accepted)
