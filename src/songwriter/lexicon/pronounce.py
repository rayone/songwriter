"""Pronunciation resolution: where each word's phones come from, and how much
to trust them.

No single source covers the vocabulary a lyricist needs. CMUdict has gold
phones for ~126k words and no modern slang. Wiktionary has IPA for ~99k words
including 'shawty', 'gyatt' and 'rizz', but converting it is 83.0% accurate
rather than perfect. Morphological rules handle the g-dropped and contracted
forms that dominate sung English. Each source is individually insufficient and
collectively enough.

So a pronunciation carries its provenance, and confidence comes from
**agreement between independent sources** rather than from trusting any one.
Two sources that derive the same rime by different routes corroborate each
other; one source alone is a hypothesis. That is the verification requirement
made concrete, and it is measurable: for words CMUdict knows, every other tier
can be scored against it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum

from ..phonetics.core import is_vowel, stress_of, strip_stress
from ..phonetics.ipa import ipa_to_arpabet


class Tier(IntEnum):
    """Pronunciation source, ordered by measured reliability.

    The ordering is empirical, not assumed. CMUdict is hand-curated. Derivation
    from a CMUdict stem applies a documented transform to gold phones. IPA
    conversion measures 83.0% rime-exact against CMUdict. Corpus induction is
    evidence from observed rhyme pairs, strong in aggregate but noisy per word.
    """

    CMUDICT = 1           # hand-curated gold
    DERIVED = 2           # documented transform of a CMUdict pronunciation
    WIKTIONARY_IPA = 3    # converted IPA, 83.0% rime accuracy
    WIKTIONARY_RHYME = 4  # Wiktionary's own rhyme key: rime only, no full word
    CORPUS = 5            # induced from observed line-end rhyme pairs
    LLM = 6               # model-proposed, never trusted alone

    @property
    def label(self) -> str:
        return self.name.lower()


class Confidence(IntEnum):
    """How much independent support a rime has."""

    CONFLICTED = 0   # sources disagree: stored, flagged, never served
    SINGLE = 1       # one source: stored, excluded from default results
    VERIFIED = 2     # two or more independent sources agree on the rime


@dataclass(frozen=True)
class Pronunciation:
    """One pronunciation of one word, with where it came from."""

    phones: tuple[str, ...]
    tier: Tier
    accent: str = "en-US"
    #: How this was obtained, for auditing: 'cmudict', 'g-drop of frustrating',
    #: 'ipa /ˈʃɔːti/', 'corpus: rhymed with naughty x412'.
    note: str = ""
    #: A colloquial variant (g-dropped, contracted) rather than the citation
    #: form. Both are real; this marks which is primary.
    is_variant: bool = False
    #: These phones are only the word's RIME, not its whole pronunciation.
    #:
    #: Wiktionary's rhyme field gives '-aɪt' for 'knight', which is the rime
    #: alone with no onset. Such a fragment still corroborates a converted-IPA
    #: rime, which is its value -- but it must never be stored as a
    #: pronunciation. Doing so gave 'knight' and 'right' the identical phone
    #: key 'AY T', so rich rhyme (homophone) matching reported them as
    #: homophones along with 'light' and 'fight'.
    is_rime_only: bool = False

    @property
    def syllables(self) -> int:
        return sum(1 for p in self.phones if is_vowel(p))

    @property
    def rime(self) -> str:
        """Phones from the last stressed vowel onward, stress digits dropped.

        The rhyme-bearing portion, and the unit sources are compared on: two
        sources may legitimately disagree about an unstressed prefix while
        agreeing on what the word rhymes with.
        """
        vowels = [i for i, p in enumerate(self.phones) if is_vowel(p)]
        if not vowels:
            return ""
        stressed = [i for i in vowels if stress_of(self.phones[i]) in (1, 2)]
        anchor = max(stressed) if stressed else vowels[0]
        return " ".join(strip_stress(p) for p in self.phones[anchor:])

    @property
    def arpabet(self) -> str:
        return " ".join(self.phones)


@dataclass
class WordPronunciations:
    """Every pronunciation found for one word, plus the verification verdict."""

    lemma: str
    prons: list[Pronunciation] = field(default_factory=list)

    @property
    def rimes(self) -> set[str]:
        return {p.rime for p in self.prons if p.rime}

    @property
    def tiers(self) -> set[Tier]:
        return {p.tier for p in self.prons}

    @property
    def best(self) -> Pronunciation | None:
        """Lowest-tier (most reliable) citation pronunciation."""
        candidates = [p for p in self.prons if not p.is_variant] or self.prons
        return min(candidates, key=lambda p: p.tier) if candidates else None

    @property
    def confidence(self) -> Confidence:
        """Verdict from cross-source agreement.

        CMUdict alone counts as verified: it is hand-curated, and demanding
        corroboration for 126k gold entries would discard the most reliable
        data in the system. Every other single source is a hypothesis.

        Multiple pronunciations of one word are not automatically a conflict --
        English has real variants ('either', 'data'). A conflict is when
        sources that should agree do not, which is judged on the rime.
        """
        if not self.prons:
            return Confidence.CONFLICTED
        if Tier.CMUDICT in self.tiers:
            return Confidence.VERIFIED

        independent = {p.tier for p in self.prons if p.rime}
        if len(independent) < 2:
            return Confidence.SINGLE

        # Two or more sources: agreement on any rime verifies, total
        # disagreement conflicts.
        by_tier: dict[Tier, set[str]] = {}
        for p in self.prons:
            if p.rime:
                by_tier.setdefault(p.tier, set()).add(p.rime)
        tier_list = list(by_tier.values())
        for i, first in enumerate(tier_list):
            for second in tier_list[i + 1:]:
                if first & second:
                    return Confidence.VERIFIED
        return Confidence.CONFLICTED


# -- morphological derivation -----------------------------------------
#
# These transforms are the largest single source of pronunciations beyond the
# dictionaries: 5,481 of the 18,677 OOV words at corpus frequency >= 100 are
# reachable this way, and they are reliable because they start from gold phones
# and apply one documented change.

#: ARPAbet suffixes for productive endings, used when a derived word is absent
#: from every dictionary but its stem is present.
_SUFFIX_PHONES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("lessness", ("L", "AH0", "S", "N", "AH0", "S")),
    ("fulness", ("F", "AH0", "L", "N", "AH0", "S")),
    ("lessly", ("L", "AH0", "S", "L", "IY0")),
    ("fully", ("F", "AH0", "L", "IY0")),
    ("ingly", ("IH0", "NG", "L", "IY0")),
    ("ness", ("N", "AH0", "S")),
    ("less", ("L", "AH0", "S")),
    ("ment", ("M", "AH0", "N", "T")),
    ("ship", ("SH", "IH0", "P")),
    ("ful", ("F", "AH0", "L")),
)

_VOICELESS = frozenset({"P", "T", "K", "F", "TH", "S", "SH", "CH"})
_SIBILANT = frozenset({"S", "Z", "SH", "ZH", "CH", "JH"})


def g_dropped(phones: tuple[str, ...]) -> tuple[str, ...] | None:
    """Turn a final ``-ing`` into ``-in'``, as sung English almost always does.

    'frustrating' ends ``IH0 NG``; sung, it ends ``IH0 N``. Without this the
    citation form can never rhyme with 'bussin'' or 'trust in', which is most
    of the rhyming that actually happens in lyrics.

    Returns None when the word does not end in NG.
    """
    if not phones or strip_stress(phones[-1]) != "NG":
        return None
    return phones[:-1] + ("N",)


def plural_of(phones: tuple[str, ...]) -> tuple[str, ...]:
    """Add the English plural/third-person suffix with correct voicing.

    ``S`` after a voiceless consonant ('cats'), ``Z`` after voiced ('dogs'),
    ``IH0 Z`` after a sibilant ('buses'). Getting this wrong changes the rime
    and so changes what the word rhymes with.
    """
    if not phones:
        return phones
    last = strip_stress(phones[-1])
    if last in _SIBILANT:
        return phones + ("IH0", "Z")
    if last in _VOICELESS:
        return phones + ("S",)
    return phones + ("Z",)


def past_of(phones: tuple[str, ...]) -> tuple[str, ...]:
    """Add the ``-ed`` suffix with correct voicing.

    ``T`` after voiceless ('walked'), ``AH0 D`` after T/D ('wanted'),
    ``D`` otherwise ('played').
    """
    if not phones:
        return phones
    last = strip_stress(phones[-1])
    if last in {"T", "D"}:
        return phones + ("AH0", "D")
    if last in _VOICELESS:
        return phones + ("T",)
    return phones + ("D",)


#: Orthographic repairs that map a sung spelling onto a dictionary word. Each
#: returns candidate spellings to look up, cheapest first.
_APOSTROPHE = re.compile(r"'")


def spelling_candidates(word: str) -> list[tuple[str, str]]:
    """Dictionary spellings that might explain ``word``, with the rule used.

    Covers the shapes that dominate lyrics: g-dropping ('lovin'' -> 'loving'),
    dropped apostrophes ('dont' -> 'don't'), added ones, elongations
    ('ooo' -> 'oo'), and possessives. Returns pairs of (candidate, rule) so the
    resulting pronunciation records how it was reached.
    """
    out: list[tuple[str, str]] = []
    if word.endswith("in'"):
        out.append((word[:-3] + "ing", "g-drop"))
    elif word.endswith("in"):
        out.append((word + "g", "g-drop"))

    if "'" in word:
        out.append((_APOSTROPHE.sub("", word), "strip-apostrophe"))
    else:
        # 'dont' -> "don't", 'thats' -> "that's": try each insertion point,
        # rightmost first, since that is where English contractions sit.
        for i in range(len(word) - 1, 0, -1):
            out.append((word[:i] + "'" + word[i:], "insert-apostrophe"))

    if word.endswith("'s"):
        out.append((word[:-2], "possessive"))

    squashed = re.sub(r"(.)\1{1,}", r"\1", word)
    if squashed != word:
        out.append((squashed, "collapse-repeats"))

    return out


def suffix_decompositions(word: str) -> list[tuple[str, tuple[str, ...], str]]:
    """Split ``word`` into (stem, suffix phones, rule) for productive endings.

    Also tries stem spellings that English orthography alters: 'jaded' from
    'jade' needs the silent e restored, 'runnin' from 'run' needs the doubled
    consonant undone.
    """
    out: list[tuple[str, tuple[str, ...], str]] = []
    for suffix, phones in _SUFFIX_PHONES:
        if not word.endswith(suffix) or len(word) <= len(suffix) + 2:
            continue
        stem = word[: -len(suffix)]
        for candidate in _stem_spellings(stem):
            out.append((candidate, phones, f"-{suffix}"))
    return out


def _stem_spellings(stem: str) -> list[str]:
    out = [stem]
    if not stem.endswith("e"):
        out.append(stem + "e")
    if len(stem) > 2 and stem[-1] == stem[-2]:
        out.append(stem[:-1])
    if stem.endswith("i"):
        out.append(stem[:-1] + "y")
    return out


_VOWEL_LETTERS = frozenset("aeiou")


def _inflected_spellings(lemma: str, suffix: str) -> set[str]:
    """Spellings ``lemma + suffix`` can take under English orthography.

    Concatenation alone is not enough: 'run' + 'ing' is written 'running', not
    'runing'. The spelling changes but the *phones* do not, so these variants
    only need recognising, not pronouncing differently.
    """
    out = {lemma + suffix}
    if not suffix:
        return out

    # silent e dropped before a vowel-initial suffix: make + ing -> making
    if lemma.endswith("e") and suffix[0] in "aeiouy":
        out.add(lemma[:-1] + suffix)

    # final consonant doubled after a single vowel: run + ing -> running
    if (len(lemma) >= 3
            and lemma[-1] not in _VOWEL_LETTERS
            and lemma[-1] not in "wxy"
            and lemma[-2] in _VOWEL_LETTERS
            and lemma[-3] not in _VOWEL_LETTERS):
        out.add(lemma + lemma[-1] + suffix)

    # y -> i after a consonant: carry + ed -> carried, happy + ly -> happily
    if len(lemma) > 2 and lemma.endswith("y") and lemma[-2] not in _VOWEL_LETTERS:
        if suffix[0] != "i":
            out.add(lemma[:-1] + "i" + suffix)
        if suffix == "s":
            out.add(lemma[:-1] + "ies")

    return out


#: Suffix spellings grouped by the phonological rule that realises them.
_INFLECTION_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("s", "es"), "plural"),
    (("ing",), "progressive"),
    (("ed", "d"), "past"),
    (("ly",), "adverb"),
    (("er", "r"), "comparative"),
    (("est", "st"), "superlative"),
)


def inflect(base: tuple[str, ...], form: str,
            lemma: str) -> tuple[str, ...] | None:
    """Derive an inflected form's phones from its lemma's.

    Wiktionary lists 224,749 inflected forms that never appear as headwords
    ('ran', 'runs', 'running'), giving the spelling and its lemma but no
    pronunciation of its own. Regular inflections are derivable from the
    lemma's phones; irregular ones ('ran' from 'run', 'children' from 'child')
    are not, and return None rather than a guess.
    """
    if not base or not form or form == lemma:
        return None

    for suffixes, rule in _INFLECTION_RULES:
        for suffix in suffixes:
            if form not in _inflected_spellings(lemma, suffix):
                continue
            if rule == "plural":
                return plural_of(base)
            if rule == "progressive":
                return base + ("IH0", "NG")
            if rule == "past":
                return past_of(base)
            if rule == "adverb":
                return base + ("L", "IY0")
            if rule == "comparative":
                return base + ("ER0",)
            if rule == "superlative":
                return base + ("AH0", "S", "T")
    return None
