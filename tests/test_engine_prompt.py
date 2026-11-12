"""Consumer-visible vocal identity in engine prompts."""

import pytest

from songwriter.music.engine_prompt import render_songbench


@pytest.mark.parametrize(
    ("vocal", "is_duet"),
    [("airy female vocal", False), ("male and female vocals", True)],
)
def test_songbench_distinguishes_female_solo_from_duet(vocal, is_duet):
    prompt = render_songbench("Electropop", vocal=vocal)
    assert ("duet" in prompt.casefold()) is is_duet
