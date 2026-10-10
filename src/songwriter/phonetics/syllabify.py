"""Syllabification of ARPAbet phone sequences.

CMUdict gives phones but no syllable boundaries. Every rhyme key in this
project is defined relative to a syllable (nucleus, onset, coda), so without
real boundaries the keys are guesses.

This is not a cosmetic concern. The engine previously computed the "coda" of
'sunflower' (S AH1 N F L AW2 ER0) as ``N F L`` by taking every consonant
between the stressed vowel and the next vowel. ``N`` closes 'sun' and ``F L``
opens 'flower', so that string spans a syllable boundary and describes no
real phonological unit. It made 'sunflower' slant-rhyme 'frustrating'
(``S T R``) on a shared final liquid.

The fix is structural rather than a tighter comparison rule: once boundaries
are explicit, a coda is only ever the consonants that actually close its own
syllable, and ``N F L`` is unconstructible.

Method: Onset Maximalism constrained by English phonotactics. Between two
vowels, assign the longest *legal English onset* to the following syllable
and leave the remainder as the coda of the preceding one. Legality is decided
by an explicit table (:data:`LEGAL_ONSETS`), not by a maximal-onset heuristic,
because unconstrained maximalism produces illegal onsets like ``N F L`` and
``T L``. The Sonority Sequencing Principle is applied as a secondary check.
"""

from __future__ import annotations

from dataclasses import dataclass

from .core import is_vowel, strip_stress, stress_of

# -- English phonotactics ---------------------------------------------

#: Consonants that may stand alone as an onset. Every English consonant can,
#: except ``NG``, which never begins a syllable in English ('sing' has it as a
#: coda; no English word starts with it).
SINGLE_ONSETS = frozenset({
    "P", "B", "T", "D", "K", "G", "F", "V", "TH", "DH", "S", "Z", "SH", "ZH",
    "CH", "JH", "M", "N", "L", "R", "W", "Y", "HH",
})

#: Legal two-consonant onsets. Enumerated rather than derived: English permits
#: ``F L`` but not ``V L``, ``S M`` but not ``S N G``, and rules that try to
#: capture this by sonority alone admit clusters that do not occur.
LEGAL_ONSETS_2 = frozenset({
    # stop + liquid
    "P L", "P R", "B L", "B R", "T R", "D R", "K L", "K R", "G L", "G R",
    # voiceless fricative + liquid
    "F L", "F R", "TH R", "SH R",
    # S + voiceless stop
    "S P", "S T", "S K",
    # S + nasal / fricative / liquid
    "S M", "S N", "S F", "S L",
    # consonant + glide
    "P Y", "B Y", "T W", "D W", "K W", "G W", "K Y", "G Y", "M Y", "N Y",
    "F Y", "V Y", "TH W", "S W", "S Y", "SH W", "HH W", "HH Y", "L Y", "R Y",
    "Z W",
})

#: Legal three-consonant onsets. In English these are exactly ``S`` plus a
#: voiceless stop plus a liquid or glide.
LEGAL_ONSETS_3 = frozenset({
    "S P L", "S P R", "S T R", "S K L", "S K R", "S K W", "S P Y", "S K Y",
    "S T Y", "S M Y", "S HH M", "SH T R", "SH P R", "SH T S",
})

#: Onsets found only in borrowed words but attested in CMUdict: 'schlep',
#: 'schmuck', 'shnook', 'tsunami', 'bwana', 'knish', 'Vladimir', 'Dvorak'.
#:
#: These must be admitted rather than rejected. Word-initial consonants are
#: *given* by the dictionary -- every consonant before the first vowel is that
#: syllable's onset by definition, with no alternative division available. All
#: 554 illegal onsets found when validating against CMUdict were word-initial
#: for exactly this reason, and zero were medial split errors. Rejecting them
#: would not fix a split; it would only mislabel real data as invalid.
LOANWORD_ONSETS = frozenset({
    "SH L", "SH M", "SH N", "SH T", "SH V", "SH P", "SH K", "SH R",
    "S V", "S B", "S G", "S R", "Z V", "Z B", "Z L", "Z N", "Z M", "Z G",
    "T S", "T L", "D Y", "D V", "D Z", "D M", "D N",
    "B W", "G W", "K W", "K N", "K V", "K T", "K S", "G N", "G D", "G B",
    "P S", "P F", "P T", "P W", "P N", "F N", "F T", "V L", "V R", "V N",
    "M N", "M B", "N Y", "HH R", "HH L", "HH N", "HH M",
    "TH L", "TH M", "TH N", "CH R", "CH L", "CH W", "CH M", "CH N",
    "JH R", "JH W", "NG G", "L M", "R T", "W R", "Y T",
})

#: All legal onsets, keyed by the space-joined cluster.
LEGAL_ONSETS = (
    frozenset(SINGLE_ONSETS) | LEGAL_ONSETS_2 | LEGAL_ONSETS_3
)

#: Sonority ranks, low (obstruent) to high (glide). A well-formed onset rises
#: in sonority toward the nucleus; a well-formed coda falls away from it.
#: ``S`` clusters are the standard exception and are handled by the tables.
_SONORITY = {
    "P": 1, "B": 1, "T": 1, "D": 1, "K": 1, "G": 1,
    "F": 2, "V": 2, "TH": 2, "DH": 2, "S": 2, "Z": 2, "SH": 2, "ZH": 2,
    "HH": 2,
    "CH": 2, "JH": 2,
    "M": 3, "N": 3, "NG": 3,
    "L": 4, "R": 4,
    "W": 5, "Y": 5,
}


def sonority(phone: str) -> int:
    """Sonority rank of a consonant. Unknown phones sort as obstruents."""
    return _SONORITY.get(strip_stress(phone), 1)


#: Phones that can never begin an English syllable. ``NG`` only ever closes
#: one, which is why 'singer' divides SING.ER with an onsetless second
#: syllable rather than SI.NGER.
NEVER_ONSET = frozenset({"NG"})


def is_legal_onset(cluster: tuple[str, ...]) -> bool:
    """Whether ``cluster`` can begin a *native* English syllable.

    Deliberately strict: this decides medial splits, and admitting marginal
    clusters here is what allowed ``N F L`` to be treated as an onset. Use
    :func:`onset_is_native` to describe a word-initial cluster, which is
    given by the dictionary rather than chosen.
    """
    if not cluster:
        return True
    if len(cluster) > 3:
        return False
    return " ".join(strip_stress(p) for p in cluster) in LEGAL_ONSETS


def onset_is_native(cluster: tuple[str, ...]) -> bool:
    """Whether ``cluster`` is a native or common-loanword English onset.

    Reports on a *word-initial* cluster without constraining it. Such a cluster
    is given by the dictionary rather than chosen by this module: every
    consonant before the first vowel belongs to that syllable, and there is no
    alternative division available. CMUdict also records thousands of foreign
    proper nouns ('mraz', 'khmer', 'zdrojewski', 'fuentes') whose onsets follow
    other languages' phonotactics, so a False result here means "borrowed",
    not "wrong".

    The invariant that genuinely tests this module is :func:`is_legal_onset`
    applied to *medial* onsets, which are the only ones the code decides.
    """
    if is_legal_onset(cluster):
        return True
    return " ".join(strip_stress(p) for p in cluster) in LOANWORD_ONSETS


@dataclass(frozen=True)
class Syllable:
    """One syllable: onset + nucleus + coda, with its stress."""

    onset: tuple[str, ...]
    nucleus: str
    coda: tuple[str, ...]

    @property
    def stress(self) -> int:
        """0 unstressed, 1 primary, 2 secondary."""
        s = stress_of(self.nucleus)
        return s if s is not None else 0

    @property
    def stress_name(self) -> str:
        return {0: "none", 1: "primary", 2: "secondary"}[self.stress]

    @property
    def phones(self) -> tuple[str, ...]:
        return self.onset + (self.nucleus,) + self.coda

    @property
    def rime(self) -> tuple[str, ...]:
        """Nucleus + coda. The rhyming unit of a single syllable."""
        return (self.nucleus,) + self.coda

    def onset_str(self) -> str:
        return " ".join(strip_stress(p) for p in self.onset)

    def nucleus_str(self) -> str:
        return strip_stress(self.nucleus)

    def coda_str(self) -> str:
        return " ".join(strip_stress(p) for p in self.coda)

    def __str__(self) -> str:
        return "".join((self.onset_str(), self.nucleus_str(), self.coda_str()))


def syllabify(phones: tuple[str, ...] | list[str]) -> tuple[Syllable, ...]:
    """Split an ARPAbet sequence into syllables.

    One syllable per vowel, which is the definition of a syllable nucleus in
    English. Consonants before the first vowel are that syllable's onset;
    consonants after the last vowel are the final coda. For each intervocalic
    cluster, the longest legal English onset goes to the following syllable
    and whatever precedes it closes the current one.

    Returns an empty tuple for input with no vowel (e.g. ``('HH', 'M')``),
    since such a sequence has no nucleus to build a syllable around.

    >>> [str(s) for s in syllabify(('S','AH1','N','F','L','AW2','ER0'))]
    ['SAHN', 'FLAW', 'ER']
    """
    phones = tuple(phones)
    vowel_idx = [i for i, p in enumerate(phones) if is_vowel(p)]
    if not vowel_idx:
        return ()

    out: list[Syllable] = []
    for n, vi in enumerate(vowel_idx):
        # Onset: for the first syllable, everything before the vowel.
        # Otherwise it is decided by the previous iteration's split.
        if n == 0:
            onset = phones[:vi]
        else:
            onset = _pending_onset

        if n + 1 < len(vowel_idx):
            # Intervocalic consonants, to divide between this coda and the
            # next onset.
            cluster = phones[vi + 1:vowel_idx[n + 1]]
            coda, _pending_onset = _split_cluster(cluster)
        else:
            # Final syllable takes every remaining consonant as its coda.
            coda = phones[vi + 1:]

        out.append(Syllable(onset=tuple(onset), nucleus=phones[vi],
                            coda=tuple(coda)))
    return tuple(out)


def _split_cluster(cluster: tuple[str, ...]) -> tuple[tuple[str, ...],
                                                      tuple[str, ...]]:
    """Divide intervocalic consonants into (coda, next_onset).

    Onset Maximalism: the next syllable claims as much as English allows, so
    'fl' in 'sunflower' opens a syllable rather than closing one. Constrained
    by :data:`LEGAL_ONSETS`, which is what stops ``N F L`` from being treated
    as an onset and keeps ``N`` where it belongs, closing 'sun'.

    A vowel-adjacent cluster must leave the next syllable an onset if it can:
    English has no zero-onset syllable in the middle of a word when a
    consonant is available (V.CV is not a possible division of 'city').
    """
    if not cluster:
        return (), ()

    # Tier 1: longest legal onset that also leaves a well-formed coda. This is
    # the normal path -- 'sunflower' takes 'F L' and leaves 'N'.
    for take in range(min(3, len(cluster)), 0, -1):
        candidate = cluster[-take:]
        if not is_legal_onset(candidate):
            continue
        if _is_valid_coda(cluster[:-take]):
            return cluster[:-take], candidate

    # Tier 2: legal onset, relaxing the coda's sonority profile. A medial
    # syllable with no onset is a stronger (and usually wrong) claim than an
    # unusual coda, so given CMUdict's reduced pronunciations -- 'admirable'
    # as AE1 D M R AH0 B AH0 L, where the cluster 'D M R' has no division
    # that satisfies both constraints -- prefer keeping the onset.
    for take in range(min(3, len(cluster)), 0, -1):
        if is_legal_onset(cluster[-take:]):
            return cluster[:-take], cluster[-take:]

    # Tier 3: nothing can open the next syllable, so the whole cluster closes
    # this one. Correct and required for NG, which never onsets in English:
    # 'singer' (S IH1 NG ER0) must divide SING.ER, not SI.NGER.
    return cluster, ()


def _is_valid_coda(coda: tuple[str, ...]) -> bool:
    """Whether ``coda`` can close an English syllable.

    Codas are more permissive than onsets ('sixths' is ``S IH K S TH S``), so
    this checks sonority rather than enumerating clusters: sonority should fall
    away from the nucleus. ``S``/``Z`` and coronal stops may appear as
    appendices past that limit ('asks', 'texts'), so they are exempt.
    """
    if len(coda) <= 1:
        return True
    _APPENDIX = {"S", "Z", "T", "D", "TH"}
    core = list(coda)
    while len(core) > 1 and strip_stress(core[-1]) in _APPENDIX:
        core.pop()
    for a, b in zip(core, core[1:]):
        if sonority(a) < sonority(b):
            return False
    return True


# -- derived keys -----------------------------------------------------

def rhyme_anchor(syllables: tuple[Syllable, ...]) -> int:
    """Index of the syllable that carries the rhyme.

    The LAST stressed syllable, counting secondary stress. CMUdict marks a
    compound's rhyming syllable as secondary ('sunflower' stresses 'sun' as
    primary and 'flow' as secondary), so ignoring stress 2 anchors the rhyme
    on the wrong syllable. Falls back to the final syllable when nothing is
    marked as stressed.
    """
    if not syllables:
        return 0
    last = 0
    for i, s in enumerate(syllables):
        if s.stress in (1, 2):
            last = i
    return last
