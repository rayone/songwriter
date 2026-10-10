"""Lexicon assembly: which strings are words, and what they sound like.

Separate from :mod:`songwriter.phonetics`, which analyses sound given phones.
This package decides *what is in the dictionary at all* and where each
pronunciation came from.
"""

from .vocab import (
    CORPUS_FLOOR,
    MAX_PHRASE_TOKENS,
    RejectionStats,
    Source,
    VocabEntry,
    admit,
    is_wellformed,
    normalise_lemma,
)

__all__ = [
    "CORPUS_FLOOR",
    "MAX_PHRASE_TOKENS",
    "RejectionStats",
    "Source",
    "VocabEntry",
    "admit",
    "is_wellformed",
    "normalise_lemma",
]
