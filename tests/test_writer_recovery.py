"""Step 1: the writer never loses a section to a cut-off or empty reply.

A reply cut off at the token limit, or an empty one, is retried in place with a
bigger budget before the turn is judged, so a truncated answer never drops a
section. _chat still reports finish_reason and usage, and still accepts every
parameter its callers pass (top_p is used by the whole-song path).

Run: uv run pytest tests/test_writer_recovery.py
"""
from __future__ import annotations

import pytest

from songwriter.ai.cowriter import SongwriterCowriter

# One 2-line AABB verse: a single writer turn. No validation_mode key means the
# default auto_llm path (writer + critic); cut-off recovery is mode-independent.
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


def test_chat_passes_top_p_and_fills_meta(cowriter: SongwriterCowriter, monkeypatch: pytest.MonkeyPatch) -> None:
    # The whole-song path calls _chat(..., top_p=...): the signature must accept it.
    seen: dict[str, object] = {}

    def fake_send(self, payload, *, timeout, meta=None):  # noqa: ANN001
        seen["payload"] = payload
        if meta is not None:
            meta["finish_reason"] = "stop"
            meta["usage"] = {"completion_tokens": 7}
            meta["model"] = payload["model"]
        return "a line"

    monkeypatch.setattr(SongwriterCowriter, "_post_chat", fake_send, raising=False)
    meta: dict[str, object] = {}
    # Exercise the real signature, including top_p, the way cowriter.py:1955 does.
    out = cowriter._chat(
        [{"role": "user", "content": "hi"}],
        model="m", temperature=0.5, top_p=0.8, max_tokens=50, meta=meta,
    )
    assert out == "a line"
    assert meta["finish_reason"] == "stop" and meta["usage"]["completion_tokens"] == 7


def test_cut_off_reply_is_retried_with_a_bigger_budget(
    cowriter: SongwriterCowriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    # First writer call is cut off at the limit; the second, with more room, lands.
    budgets: list[int] = []
    replies = iter([
        ("Line 1: Take my heart", "length"),  # truncated: cut off after one line
        ("Line 1: Take my heart\nLine 2: Break me apart", "stop"),
    ])

    def chat(messages, **kw):  # noqa: ANN001
        if _is_critic(messages):
            return '{"passed": true, "summary": "ok", "lines": []}'
        budgets.append(int(kw.get("max_tokens", 0)))
        text, reason = next(replies, ("Line 1: Take my heart\nLine 2: Break me apart", "stop"))
        if (meta := kw.get("meta")) is not None:
            meta["finish_reason"] = reason
            meta["usage"] = {}
        return text

    monkeypatch.setattr(cowriter, "_chat", chat)
    events = list(cowriter.stream_song_interactive(max_retries=1, **_LOVE))

    # The second writer call used a larger budget than the first, in one attempt.
    assert len(budgets) >= 2 and budgets[1] > budgets[0]
    resp = next(e for e in events if e.get("type") == "response")
    assert resp["cut_off"] is False
    assert [e["line"] for e in events if e.get("type") == "line_accepted"] == ["Take my heart", "Break me apart"]


def test_full_count_but_truncated_reply_is_retried_not_accepted(
    cowriter: SongwriterCowriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first reply parses to the full line count but was cut off at the limit,
    # so its last line ("Break me") is a truncated fragment. Count alone must not
    # accept it: the turn retries with a bigger budget and takes the whole reply.
    budgets: list[int] = []
    replies = iter([
        ("Line 1: Take my heart\nLine 2: Break me", "length"),  # 2 lines, but cut off
        ("Line 1: Take my heart\nLine 2: Break me apart", "stop"),
    ])

    def chat(messages, **kw):  # noqa: ANN001
        if _is_critic(messages):
            return '{"passed": true, "summary": "ok", "lines": []}'
        budgets.append(int(kw.get("max_tokens", 0)))
        text, reason = next(replies, ("Line 1: Take my heart\nLine 2: Break me apart", "stop"))
        if (meta := kw.get("meta")) is not None:
            meta["finish_reason"] = reason
            meta["usage"] = {}
        return text

    monkeypatch.setattr(cowriter, "_chat", chat)
    events = list(cowriter.stream_song_interactive(max_retries=1, **_LOVE))

    assert len(budgets) >= 2 and budgets[1] > budgets[0]
    assert [e["line"] for e in events if e.get("type") == "line_accepted"] == ["Take my heart", "Break me apart"]


def test_all_empty_replies_flag_the_section_cut_off(
    cowriter: SongwriterCowriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every writer reply is cut off before any line: the turn says so, not "got 0".
    def chat(messages, **kw):  # noqa: ANN001
        if _is_critic(messages):
            return '{"passed": true, "summary": "ok", "lines": []}'
        if (meta := kw.get("meta")) is not None:
            meta["finish_reason"] = "length"
            meta["usage"] = {}
        return "Line"

    monkeypatch.setattr(cowriter, "_chat", chat)
    events = list(cowriter.stream_song_interactive(max_retries=1, **_LOVE))

    resp = next(e for e in events if e.get("type") == "response")
    assert resp["cut_off"] is True
    val = next(e["validation"] for e in events if e.get("type") == "validation")
    assert any("cut off at the token limit" in i for i in val["issues"])
