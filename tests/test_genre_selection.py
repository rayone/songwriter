"""Genre choices stay independent of the reference catalog and survive generation."""
from types import SimpleNamespace
from unittest.mock import Mock

import duckdb
import pytest

from songwriter.ai.cowriter import SongwriterCowriter
from songwriter.ai.generation import SongGeneration
from songwriter.corpus.references import ReferenceStore
from songwriter.db import Database


@pytest.fixture
def cowriter():
    writer = SongwriterCowriter.__new__(SongwriterCowriter)
    writer.studio_db = SimpleNamespace(is_available=False)
    writer._vocab = {'vocal': ['lead']} 
    writer.resolve_defaults = Mock(return_value={
        'genre': 'Pop', 'bpm': 128, 'music_key': 'C', 'scale': 'minor',
        'banned_words': [], 'rhyme_scheme': 'AABB', 'section_plan': 'standard',
        'vocal': 'lead', 'syllable_target': '8-10',
    })
    writer._craft_questions = Mock(return_value=None)
    writer._chat = Mock(return_value='{"genre": "Melodic House"}')
    writer._style_tokens = Mock(return_value={'bpm_hint': None})
    return writer


def test_conflict_has_three_choices_and_keeps_original_default(cowriter):
    song = SongGeneration(genre='Progressive House / Vocal Trance', reference_song={'genre': 'Pop'}, model='test-model')
    data = cowriter.interview(song)
    question = next(q for q in data.questions if q['id'] == 'genre')
    assert question['options'] == ['Progressive House / Vocal Trance', 'Pop', 'Melodic House']
    assert question['default'] == 'Progressive House / Vocal Trance'
    assert question['category'] == 'creative'
    assert question['option_labels']['Melodic House'] == 'AI recommendation'
    assert data.answers['genre'] == 'Progressive House / Vocal Trance'
    assert 'Progressive House / Vocal Trance' in cowriter._chat.call_args.args[0][0]['content']


@pytest.mark.parametrize('reference', [None, {'genre': 'indie'}, {'genre': ''}])
def test_matching_or_missing_reference_never_forces_pop(cowriter, reference):
    song = SongGeneration(genre='Indie', reference_song=reference, model='test-model')
    data = cowriter.interview(song, use_model=False)
    assert data.genre == 'Indie'
    assert not any(q['id'] == 'genre' for q in data.questions)
    cowriter._chat.assert_not_called()


@pytest.mark.parametrize('response', ['not json', '{"genre": "Pop"}', '{"genre": "Indie"}', '{"genre": 42}'])
def test_bad_recommendations_have_honestly_labelled_fallback(cowriter, response):
    cowriter._chat.return_value = response
    song = SongGeneration(genre='Indie', reference_song={'genre': 'Pop'}, model='test-model')
    data = cowriter.interview(song)
    q = next(q for q in data.questions if q['id'] == 'genre')
    assert len(set(q['options'])) == 3
    assert q['default'] == 'Indie'
    assert q['option_labels'][q['options'][2]] == 'Suggested blend — AI unavailable'

@pytest.mark.parametrize('choice', ['Progressive House / Vocal Trance', 'Pop', 'Melodic House'])
def test_step3_choice_is_authoritative_and_roundtrips(choice):
    gen = SongGeneration(
        model='test-model',
        style='Progressive House / Vocal Trance, atmospheric synths',
        genre='Progressive House / Vocal Trance',
        reference_song={'genre': 'Pop'},
        answers={'genre': choice},
    )
    gen.resolve_inputs({'genre': 'Rock'})
    assert gen.genre == choice
    assert gen.to_record()['genre'] == choice
    assert SongGeneration.from_record(gen.to_record()).genre == choice


def test_no_step3_answer_keeps_style_ahead_of_reference():
    gen = SongGeneration(model='test-model', style='Indie Folk', reference_song={'genre': 'Pop'})
    gen.resolve_inputs({})
    assert gen.genre == 'Indie Folk'


def test_preview_and_engine_spec_use_selected_genre(cowriter, monkeypatch):
    monkeypatch.setattr('songwriter.ai.cowriter.prompt_templates.line_rules_text', lambda: '')
    song = SongGeneration(
        brief='An original song', genre='Trance', style='Trance',
        reference_song={'title': 'Reference', 'artist': 'Artist', 'genre': 'Pop'},
        answers={'genre': 'Indie Folk'}, model='test-model',
    )
    cowriter.prepare_generation(song)
    result = cowriter.build_instructions_payload(song)
    assert result.genre == 'Indie Folk'
    assert 'Indie Folk' in result.system_prompt


@pytest.mark.parametrize('genre', ['Indie', 'Progressive House', 'Jazz'])
def test_niche_corpus_songs_rank_above_curated_pop(cowriter, tmp_path, genre):
    path = tmp_path / 'corpus.duckdb'
    db = duckdb.connect(str(path))
    db.execute('''CREATE TABLE songs(song_id INTEGER, title VARCHAR, artist VARCHAR,
        genre VARCHAR, tag VARCHAR, year INTEGER, tempo_bpm DOUBLE,
        musical_key VARCHAR, scale VARCHAR, lyrics VARCHAR)''')
    for i, label in enumerate(['Pop', genre, 'Rock']):
        db.execute('INSERT INTO songs VALUES (?, ?, ?, ?, ?, 2024, 120, ?, ?, ?)',
                   [i + 1, f'Track {i}', 'An independent artist', label, label, 'C', 'major', 'Original lyric ' * 10])
    db.close()
    cowriter.studio_db = SimpleNamespace(db_path=path, is_available=True)
    cowriter._wb = SimpleNamespace(
        searcher=SimpleNamespace(
            abc_song_ids=lambda: set(),
            references=ReferenceStore(Database.get(tmp_path / 'workbench.duckdb', read_only=False)),
        ),
        search_songs=lambda *a, **k: [],
    )
    results = cowriter.recommend_reference_songs(SongGeneration(genre=genre, bpm=120, key='C'), limit=4)
    assert results[0]['genre'] == genre
    assert results[0]['title'] == 'Track 1'
    assert results[0]['target_genre'] == genre
    assert 'Structural/phrasing reference only' in results[0]['guidance']
