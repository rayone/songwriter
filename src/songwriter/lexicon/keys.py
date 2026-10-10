"""Rhyme fingerprints: indexable keys that make every rhyme type a JOIN.

A rhyme relation is a match between two words on some phonological feature. The
naive way to serve that is a precomputed pair table, but measured on this
vocabulary the five main relations come to ~1.8 billion directed edges --
alliteration alone is 866M, because English has only 147 distinct initial
onsets. Storing that is neither necessary nor useful.

Instead each pronunciation gets a row of short key columns. A rhyme query then
becomes an equality join on one indexed column, and the relation is computed at
read time rather than stored. Only relations that are *not* computable need
stored edges -- in this project that is the 47,472 human-asserted slant links
from the editorial rhyme source, which no phonetic rule reproduces.

Two hazards shape the design:

Empty keys must never match. 5,200 words (4.1% of the vocabulary) end in an
open syllable, so their coda is the empty string. Equality on that key would
assert that 'sofa' consonance-rhymes with 'tomorrow' because neither has a
coda -- 27 million false pairs from one oversight. Every key that can be empty
is therefore stored as NULL, which SQL equality never matches.

Keys are built from syllables, not phone runs. The bug that started this work
computed a "coda" by scanning from the stressed vowel to the next vowel, which
crosses a syllable boundary: 'sunflower' yielded ``N F L``, the coda of 'sun'
glued to the onset of 'flower'. Keys here are assembled from
:mod:`songwriter.phonetics.syllabify` output, so such a string cannot occur.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..phonetics.core import strip_stress
from ..phonetics.syllabify import Syllable, rhyme_anchor, syllabify

#: Consonants grouped by how they are articulated. Two codas in the same class
#: sound related without being identical, which is what a 'family' rhyme is:
#: 'cat'/'cap' both end in a voiceless stop.
CONSONANT_CLASS: dict[str, str] = {
    "P": "stop_vl", "T": "stop_vl", "K": "stop_vl",
    "B": "stop_vd", "D": "stop_vd", "G": "stop_vd",
    "F": "fric_vl", "TH": "fric_vl", "S": "fric_vl", "SH": "fric_vl",
    "HH": "fric_vl",
    "V": "fric_vd", "DH": "fric_vd", "Z": "fric_vd", "ZH": "fric_vd",
    "CH": "affric", "JH": "affric",
    "M": "nasal", "N": "nasal", "NG": "nasal",
    "L": "liquid", "R": "liquid",
    "W": "glide", "Y": "glide",
}

#: Vowels by tongue position. Used for assonance width, not for equality.
VOWEL_CLASS: dict[str, str] = {
    "IY": "front", "IH": "front", "EY": "front", "EH": "front", "AE": "front",
    "AH": "central", "ER": "central",
    "UW": "back", "UH": "back", "OW": "back", "AO": "back", "AA": "back",
    "AY": "diphthong", "AW": "diphthong", "OY": "diphthong",
}


def _blank_to_none(text: str) -> str | None:
    """Empty keys become NULL so they cannot participate in equality joins.

    This is the guard against the 27M-false-pair failure described above.
    """
    return text or None


@dataclass(frozen=True)
class RhymeKeys:
    """Every fingerprint for one pronunciation.

    Each field is the key for one family of rhyme relations. Nullable fields
    are absent rather than empty when the feature does not exist for the word.
    """

    #: Phones from the rhyme anchor to the end: the perfect-rhyme key.
    #: 'cat' -> 'AE T', 'sunflower' -> 'AW ER'.
    perfect_tail: str
    #: Syllables the perfect tail spans: 1 masculine, 2 feminine, 3 dactylic.
    perfect_depth: int
    #: Vowels only, from the anchor. 'late' and 'shape' share 'EY'.
    assonance_tail: str
    #: Consonants only, from the anchor. 'bridge' and 'grudge' share 'JH'.
    consonance_tail: str | None
    #: Articulation classes of the anchor coda: 'cat'/'cap' share 'stop_vl'.
    coda_class: str | None
    #: Anchor onset + coda with the vowel removed: pararhyme ('flesh'/'flash').
    frame_key: str | None
    #: Anchor onset + nucleus: reverse rhyme ('late'/'lake').
    reverse_key: str | None
    #: Onset of the stressed syllable, for alliteration.
    stressed_onset: str | None
    #: Onset of the first syllable, for word-initial alliteration.
    initial_onset: str | None
    #: The anchor's nucleus alone, the widest assonance key.
    nucleus: str
    #: Full phone string without stress digits: rich rhyme / homophones.
    phone_key: str
    #: Number of syllables in the whole word.
    syllables: int
    #: Stress pattern, e.g. '102' for 'sunflower'.
    stress_pattern: str

    @property
    def anchor_is_open(self) -> bool:
        """Whether the rhyme-bearing syllable itself has no coda.

        Distinct from :attr:`has_no_tail_consonants`. 'sofa' (S OW1 F AH0) has
        an open anchor -- nothing closes 'so' -- yet its tail still contains the
        F that opens the next syllable, so it can still consonance-rhyme.
        """
        return self.coda_class is None

    @property
    def has_no_tail_consonants(self) -> bool:
        """Whether the whole rhyme tail is vowels ('power', 'hour')."""
        return self.consonance_tail is None


def extract_keys(phones: tuple[str, ...]) -> RhymeKeys | None:
    """Compute every rhyme fingerprint for one pronunciation.

    Returns None for input with no vowel, which has no rhyme-bearing syllable.
    """
    syllables = syllabify(phones)
    if not syllables:
        return None

    anchor_index = rhyme_anchor(syllables)
    anchor = syllables[anchor_index]
    tail: tuple[Syllable, ...] = syllables[anchor_index:]

    # The rhyme starts at the anchor's NUCLEUS, never at its onset. Perfect
    # rhyme requires the onsets to differ ('cat'/'hat'), so including the onset
    # would turn this into an identity key: 'cat' would be 'K AE T' against
    # 'hat' as 'HH AE T' and the two would not match at all.
    tail_phones = list(anchor.rime) + [p for s in tail[1:] for p in s.phones]
    perfect_tail = " ".join(strip_stress(p) for p in tail_phones)

    vowels = [s.nucleus_str() for s in tail]
    # Consonants from the nucleus onward: the anchor's coda, then everything in
    # the syllables after it. Onsets before the anchor nucleus are excluded for
    # the same reason -- 'bridge' and 'grudge' consonance-rhyme on 'JH', not on
    # 'B R JH' against 'G R JH'.
    consonants = anchor.coda_str().split() + [
        c for s in tail[1:]
        for c in (s.onset_str().split() + s.coda_str().split())
    ]

    # The coda of the anchor syllable only -- not the word-final coda, and not
    # a run of consonants spanning a boundary.
    anchor_coda = anchor.coda_str()
    coda_classes = [CONSONANT_CLASS.get(c, c) for c in anchor_coda.split()]

    anchor_onset = anchor.onset_str()
    frame = " ".join(filter(None, (anchor_onset, anchor_coda)))
    reverse = " ".join(filter(None, (anchor_onset, anchor.nucleus_str())))

    return RhymeKeys(
        perfect_tail=perfect_tail,
        perfect_depth=len(tail),
        assonance_tail=" ".join(vowels),
        consonance_tail=_blank_to_none(" ".join(consonants)),
        coda_class=_blank_to_none(" ".join(coda_classes)),
        # Pararhyme needs BOTH an onset and a coda ('flesh'/'flash'). With no
        # coda the frame collapses to the onset alone, and matching on that is
        # alliteration, not pararhyme -- 'flow'/'flee' would otherwise pair.
        frame_key=(_blank_to_none(frame)
                   if anchor_onset and anchor_coda else None),
        reverse_key=_blank_to_none(reverse) if anchor_onset else None,
        stressed_onset=_blank_to_none(anchor_onset),
        initial_onset=_blank_to_none(syllables[0].onset_str()),
        nucleus=anchor.nucleus_str(),
        phone_key=" ".join(strip_stress(p) for p in phones),
        syllables=len(syllables),
        stress_pattern="".join(str(s.stress) for s in syllables),
    )


def spelling_tail(lemma: str, length: int = 4) -> str:
    """Final letters of a word, for eye rhyme.

    'cough', 'bough' and 'rough' share 'ough' but no sound. A minimum length
    matters: two shared letters would pair almost every word ending in 'ed'.
    """
    letters = "".join(c for c in lemma.lower() if c.isalpha())
    return letters[-length:]


#: The rhyme relations this schema supports, with the join each one needs.
#: Kept as data so the query layer and the documentation cannot drift apart.
RELATIONS: dict[str, str] = {
    "perfect_masculine":
        "perfect_tail equal, perfect_depth = 1",
    "perfect_feminine":
        "perfect_tail equal, perfect_depth = 2",
    "perfect_dactylic":
        "perfect_tail equal, perfect_depth >= 3",
    "rich":
        "phone_key equal, different lemma (homophones: knight/night)",
    "assonance":
        "assonance_tail equal, consonance_tail differs",
    "consonance":
        "consonance_tail equal and non-null, assonance_tail differs",
    "family":
        "assonance_tail equal, coda_class equal, consonance_tail differs",
    "pararhyme":
        "frame_key equal and non-null, nucleus differs",
    "reverse":
        "reverse_key equal and non-null, consonance_tail differs",
    "alliteration":
        "stressed_onset equal and non-null (a filter, never a primary lookup)",
    "eye":
        "spelling_tail equal, perfect_tail differs",
    "mosaic":
        "perfect_tail equal, one side is_phrase",
    "additive":
        "target coda extends source coda ('coal' -> 'cold')",
    "subtractive":
        "target coda is a prefix of source coda ('cold' -> 'coal')",
}
