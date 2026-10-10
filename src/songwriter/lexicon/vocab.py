"""Vocabulary assembly: decide which strings are real words worth rhyming.

The existing ``word`` table is contaminated. 22,879 rows are multi-token, and
among those are Wiktionary-style definition fragments
('a quarter of an ounce of a drug'), concatenated rhyme pairs from book parsing
('tact tracked', 'blight bright'), and OCR debris ('imeless im\\'lis') sitting
beside genuine phrases ('dolce vita', 'football pools'). That contamination
reaches the surface: a rhyme lookup for 'light' returned 'appetite bite' and
'copyright daylight' as if they were single words.

Heuristics cannot separate these. Measured on the 15,699 unattested multi-token
rows, a function-word ratio -- the obvious signal for definition text -- puts
'a quarter of an ounce of a drug' (0.62) above 'a bar' (0.50), while the junk
'tact tracked' and the real phrase 'dolce vita' both score 0.00. There is no
threshold that divides them.

So admission is decided by **independent attestation** instead. A string enters
the vocabulary when a source that did not invent it says it is a word:
CMUdict, Wiktionary, the song corpus, or a book's phrase list. That is the same
cross-source principle used for pronunciations, and it is verifiable rather
than tuned.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntFlag


class Source(IntFlag):
    """Which lexicons attest a string. Stored as a bitmask for compactness."""

    NONE = 0
    CMUDICT = 1          # gold pronunciations, ~126k words
    WIKTIONARY = 2       # Kaikki extract, 1.36M headwords incl. modern slang
    CORPUS = 4           # sung in the 3.18M-song corpus
    CORPUS_NGRAM = 8     # sung as a multi-word phrase
    BOOK_PHRASE = 16     # attested in a rhyming dictionary's phrase list
    URBAN = 32           # Urban Dictionary, above a vote threshold

    @property
    def count(self) -> int:
        return bin(self.value).count("1")


#: Tokens that carry no lexical content. Used to recognise definition
#: fragments, not to score them: a candidate made *entirely* of these is not a
#: phrase anyone rhymes.
_FUNCTION_WORDS = frozenset("""
a an the of or and to in on for with at by from as is was were be been being
that this these those it its who whom which what when where while
someone something somebody anyone anything person thing place
""".split())

#: Words that only ever appear in a definition, never in a phrase people sing.
_DEFINITIONAL = frozenset("""
esp especially usually often sometimes generally typically
abbreviation abbrev variant form plural singular past tense
someone something somebody anybody
""".split())

_LETTER_SPACED = re.compile(r"\b[a-z]( [a-z]){2,}\b")
_VALID_CHARS = re.compile(r"^[a-z0-9'\- .&/]+$")


@dataclass
class VocabEntry:
    """One admitted string, with the evidence that admitted it."""

    lemma: str
    display: str
    sources: Source = Source.NONE
    corpus_freq: int = 0

    #: Definitions, as '[pos] gloss' strings.
    #:
    #: The reason the lexicon exists rather than a bare phonetic index. A rhyme
    #: list cannot be used to write a line without them: 'knight' and 'night'
    #: are the same sound and different words, and no amount of phonetic detail
    #: distinguishes them.
    glosses: tuple[str, ...] = field(default_factory=tuple)
    #: Parts of speech, for choosing a word that fits a line's grammar.
    pos: tuple[str, ...] = field(default_factory=tuple)
    #: Homophones a dictionary states outright ('night' -> 'knight', 'nite').
    #: Editorial ground truth for rich rhyme, which is otherwise derived by
    #: comparing phone keys.
    homophones: tuple[str, ...] = field(default_factory=tuple)
    #: Semantic domains ('chess', 'astronomy'), so a rhyme can be chosen for the
    #: song's subject and not only for its sound.
    topics: tuple[str, ...] = field(default_factory=tuple)
    #: Word-to-word relations, keyed by relation name ('synonyms', 'antonyms',
    #: 'hyponyms', ...) with the related words as the value.
    #:
    #: One mapping rather than a field per relation, so a relation added upstream
    #: needs no change here. What a writer reaches for when a rhyme fits the
    #: sound but not the sense: a synonym for the same idea, an antonym for
    #: contrast, a hyponym for something more concrete.
    relations: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: A real sentence showing the word in use, which a definition does not.
    examples: tuple[str, ...] = field(default_factory=tuple)
    #: Traditional respelling, independent evidence of syllables and stress.
    respelling: str = ""

    #: Register labels present on this word: 'slang', 'informal', 'vulgar',
    #: 'offensive', 'archaic', 'dialect', 'poetic', 'proper'.
    #:
    #: A set rather than a field per label, so every label is carried by the
    #: same mechanism and none can acquire behaviour of its own. These describe;
    #: they never filter. The lexicon represents what is sung, and a consumer
    #: decides what to do with the labels.
    tags: frozenset[str] = frozenset()

    def with_tag(self, tag: str) -> None:
        """Add a register label."""
        self.tags = self.tags | {tag}

    @property
    def tokens(self) -> list[str]:
        return self.lemma.split()

    @property
    def word_count(self) -> int:
        return len(self.tokens)

    @property
    def is_phrase(self) -> bool:
        return self.word_count > 1

    @property
    def spelling_tail(self) -> str:
        """Final letters, for eye rhyme ('cough'/'rough' share 'ough')."""
        letters = re.sub(r"[^a-z]", "", self.lemma)
        return letters[-4:]

    @property
    def attestation_count(self) -> int:
        return self.sources.count


@dataclass
class RejectionStats:
    """Why candidates were turned away. Printed by the build for auditing."""

    bad_characters: int = 0
    bound_morpheme: int = 0
    letter_spaced: int = 0
    all_function_words: int = 0
    definitional: int = 0
    too_many_tokens: int = 0
    unattested_phrase: int = 0
    unattested_word: int = 0
    too_long: int = 0
    empty: int = 0

    @property
    def total(self) -> int:
        return sum(getattr(self, f) for f in self.__dataclass_fields__)

    def report(self) -> str:
        lines = [f"  {name:22} {getattr(self, name):>8,}"
                 for name in self.__dataclass_fields__
                 if getattr(self, name)]
        return "\n".join(lines)


#: A phrase longer than this is prose, not a rhymable unit. The longest real
#: entries in the corpus ('balance of power', 'miles per hour') are four
#: tokens; beyond six every sampled row was definition text.
MAX_PHRASE_TOKENS = 6

#: Single words longer than this are compounds run together or OCR errors.
MAX_LEMMA_CHARS = 40


def normalise_lemma(text: str) -> str:
    """Lowercase and collapse whitespace, preserving apostrophes and hyphens.

    Those two characters are load-bearing: stripping them would destroy
    'gon'', 'lovin'' and 'half-hour', which is most of the g-dropped slang and
    the hyphenated compounds this project needs.
    """
    return " ".join(text.lower().split())


def is_wellformed(lemma: str, stats: RejectionStats | None = None) -> bool:
    """Whether ``lemma`` is shaped like a word or phrase at all.

    Shape checks only -- no judgement about whether the string is *attested*,
    which :func:`admit` decides. Applied before attestation because these
    rejections are cheap and unambiguous.
    """
    def fail(counter: str) -> bool:
        if stats is not None:
            setattr(stats, counter, getattr(stats, counter) + 1)
        return False

    if not lemma:
        return fail("empty")
    if len(lemma) > MAX_LEMMA_CHARS:
        return fail("too_long")
    if not _VALID_CHARS.match(lemma):
        return fail("bad_characters")

    # A bound morpheme is not a word anyone sings. Wiktionary lists thousands
    # ('-ality', '-ceive', 'pre-') as real headwords with real definitions, but
    # they cannot end a line, so they cannot be a rhyme.
    if lemma.startswith("-") or lemma.endswith("-"):
        return fail("bound_morpheme")

    tokens = lemma.split()
    if len(tokens) > MAX_PHRASE_TOKENS:
        return fail("too_many_tokens")

    # 'w a l k i n g' and 'a eg a oligo a' -- a headword spelled out letter by
    # letter, which is an artefact of table parsing.
    if _LETTER_SPACED.search(lemma):
        return fail("letter_spaced")

    if len(tokens) > 1:
        if all(t in _FUNCTION_WORDS for t in tokens):
            return fail("all_function_words")
        if any(t in _DEFINITIONAL for t in tokens):
            return fail("definitional")

    return True


def admit(entry: VocabEntry, stats: RejectionStats | None = None) -> bool:
    """Whether ``entry`` has enough independent support to enter the lexicon.

    Multi-word strings need attestation from a source that did not invent
    them -- Wiktionary, the sung corpus, or a book's curated phrase list.
    CMUdict alone is not enough for a phrase, because CMUdict has no phrase
    entries, so a multi-token row reaching it came from concatenation.

    Single words are admitted by any lexicon, or by being sung often enough to
    be real regardless of what the dictionaries hold -- which is exactly how
    slang like 'shawty' and 'finna' gets in.
    """
    def fail(counter: str) -> bool:
        if stats is not None:
            setattr(stats, counter, getattr(stats, counter) + 1)
        return False

    if entry.is_phrase:
        # A phrase must be a LEXICAL UNIT, attested by a dictionary. Being a
        # frequent word sequence is not enough, and admitting n-grams on their
        # own frequency flooded the lexicon with 1.7M rows like 'i m', 'and i'
        # and 'like a'.
        #
        # The reason is not just noise. A phrase rhymes on its final stressed
        # syllable, so 'i know' rhymes exactly as 'know' does and contributes
        # nothing a rhyme lookup did not already have. Mosaic rhyme is only
        # useful when the phrase is a unit a writer could actually substitute
        # ('ivory tower', 'no cap', 'apples and pears').
        lexical = Source.WIKTIONARY | Source.BOOK_PHRASE | Source.URBAN
        if not entry.sources & lexical:
            return fail("unattested_phrase")
        return True

    if entry.sources & (Source.CMUDICT | Source.WIKTIONARY | Source.URBAN):
        return True
    # Sung, but in no dictionary: admit above a floor so real slang survives
    # while typos and one-off hapax do not.
    if entry.sources & Source.CORPUS and entry.corpus_freq >= CORPUS_FLOOR:
        return True
    return fail("unattested_word")


#: Corpus frequency a word needs to enter on song evidence alone. At 100 the
#: OOV set is 18,677 words; dropping the floor to 1 admits 246,033, which is
#: overwhelmingly misspellings and transcription noise.
CORPUS_FLOOR = 100
