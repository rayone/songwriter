"""Every setting the Settings tab sends must survive a PUT then GET. A field
missing from SettingsUpdate would be silently dropped on save; this catches that.

Run: uv run pytest tests/test_settings_api.py
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from songwriter.web.app import app
from songwriter import settings as app_settings


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Start from a clean settings cache so the round-trip reads what we wrote.
    monkeypatch.setattr(app_settings, "_cache", None)
    return TestClient(app)


# The numeric and text knobs the Settings tab posts, with a value distinct from
# the default so a dropped field shows up as the old value on read-back.
_NUMERIC = {
    "default_bpm": 101,
    "cliche_min_freq": 7777,
    "writer_max_tokens": 512,
    "syllable_tolerance": 1,
    "critic_max_tokens": 1234,
    "unseen_pair_min_expected": 42,
    "rhyme_landing_min_songs": 9,
}
_TEXT = {
    "default_music_key": "G",
    "default_scale": "minor",
    "no_end_tag_genres": "trance\nambient\nvaporwave",
    "idiom_allowlist": "right now\ncome on",
}


def test_every_ui_setting_survives_put_then_get(client: TestClient) -> None:
    body = {**_NUMERIC, **_TEXT}
    put = client.put("/api/settings", json=body)
    assert put.status_code == 200

    got = client.get("/api/settings").json()["settings"]
    for key, want in _NUMERIC.items():
        assert key in got, f"{key} missing from settings model"
        assert int(got[key]["value"]) == want, f"{key} did not persist"
    for key, want in _TEXT.items():
        assert got[key]["value"] == want, f"{key} did not persist"


def test_critic_and_writer_budgets_are_not_silently_dropped(client: TestClient) -> None:
    # The specific regression: these two were once absent from SettingsUpdate.
    client.put("/api/settings", json={"critic_max_tokens": 1500, "writer_max_tokens": 650})
    got = client.get("/api/settings").json()["settings"]
    assert int(got["critic_max_tokens"]["value"]) == 1500
    assert int(got["writer_max_tokens"]["value"]) == 650
    assert got["critic_max_tokens"]["source"] == "db"
