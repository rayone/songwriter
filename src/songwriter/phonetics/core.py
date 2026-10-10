"""Phonetic core: deterministic sound analysis. No LLM involvement.

Everything here is computed from CMUdict phonemes, not guessed from spelling.
The LLM layer above must never be asked to judge rhyme, syllables or stress --
it gets answers from this module instead.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field

import cmudict

# ARPAbet vowels carry a stress digit (0/1/2); consonants do not.
VOWEL_RE = re.compile(r"[AEIOU][A-Z]?[0-2]")
STRESS_RE = re.compile(r"([A-Z]+)([0-2])")

# Vowels that broaden before a voiceless fricative in southern British
# English (the BATH-TRAP split): 'glass' is AE in American, AA in RP.
_BATH_TRIGGERS = {"S", "F", "TH", "N", "M"}

ACCENTS = ("en-US", "en-GB-nonrhotic")


_PRODUCTIVE_SUFFIXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("lessly", ("L", "AH0", "S", "L", "IY0")),
    ("lessness", ("L", "AH0", "S", "N", "AH0", "S")),
    ("fully", ("F", "AH0", "L", "IY0")),
    ("fulness", ("F", "AH0", "L", "N", "AH0", "S")),
    ("ingly", ("IH0", "NG", "L", "IY0")),
    ("ness", ("N", "AH0", "S")),
    ("less", ("L", "AH0", "S")),
    ("ful", ("F", "AH0", "L")),
    ("ment", ("M", "AH0", "N", "T")),
    ("ship", ("SH", "IH0", "P")),
)

# Consonant similarity classes, for slant-rhyme scoring.
_CONS_CLASS = {
    "P": "stop_bilabial", "B": "stop_bilabial",
    "T": "stop_alveolar", "D": "stop_alveolar",
    "K": "stop_velar", "G": "stop_velar",
    "F": "fric_labial", "V": "fric_labial",
    "TH": "fric_dental", "DH": "fric_dental",
    "S": "fric_alveolar", "Z": "fric_alveolar",
    "SH": "fric_postalv", "ZH": "fric_postalv",
    "CH": "affricate", "JH": "affricate",
    "M": "nasal", "N": "nasal", "NG": "nasal",
    "L": "liquid", "R": "liquid",
    "W": "glide", "Y": "glide",
    "HH": "aspirate",
}

# Vowel proximity for assonance: same row = near-identical nucleus.
_VOWEL_NEIGHBOURS = {
    "IY": {"IH"}, "IH": {"IY", "EH"},
    "EH": {"IH", "AE"}, "AE": {"EH", "AH"},
    "AH": {"AE", "AA", "ER"}, "AA": {"AH", "AO"},
    "AO": {"AA", "OW"}, "OW": {"AO", "UH"},
    "UH": {"OW", "UW"}, "UW": {"UH"},
    "ER": {"AH"},
    "AY": {"EY"}, "EY": {"AY"}, "OY": {"AO"},
    "AW": {"AA"},
}

# Broad phonetic consonant families for songwriter slant/family rhyming.
_CONSONANT_FAMILY: dict[str, str] = {
    "P": "plosive", "B": "plosive", "T": "plosive", "D": "plosive", "K": "plosive", "G": "plosive",
    "F": "fricative", "V": "fricative", "TH": "fricative", "DH": "fricative",
    "S": "fricative", "Z": "fricative", "SH": "fricative", "ZH": "fricative",
    "CH": "fricative", "JH": "fricative",
    "M": "nasal", "N": "nasal", "NG": "nasal",
    "L": "liquid", "R": "liquid",
    "W": "glide", "Y": "glide",
    "HH": "aspirate",
}

# Unstressed weak-form pronunciations in connected speech / lyric mosaic rhyming.
WEAK_FORMS: dict[str, tuple[str, ...]] = {
    "to": ("T", "AH0"),
    "you": ("Y", "AH0"),
    "it": ("IH0", "T"),
    "the": ("DH", "AH0"),
    "of": ("AH0", "V"),
    "a": ("AH0",),
    "and": ("AH0", "N"),
    "for": ("F", "ER0"),
    "in": ("IH0", "N"),
    "at": ("AH0", "T"),
    "me": ("M", "IY0"),
    "my": ("M", "AY0"),
    "be": ("B", "IY0"),
    "on": ("AO0", "N"),
}

def estimate_syllables(word: str) -> int:
    """Orthographic vowel-cluster syllable estimation for words missing from CMUdict."""
    w = word.strip().lower()
    if not w:
        return 0
    if "-" in w:
        return sum(estimate_syllables(part) for part in w.split("-") if part)
    w_clean = re.sub(r"[^a-z']", "", w)
    if not w_clean:
        return 1
    if w_clean.endswith("e") and not w_clean.endswith(("le", "ee")) and len(w_clean) > 2:
        w_stem = w_clean[:-1]
    else:
        w_stem = w_clean
    matches = re.findall(r"[aeiouy]+", w_stem)
    return max(1, len(matches))


def _load_dict() -> dict[str, list[list[str]]]:
    return cmudict.dict()


@functools.lru_cache(maxsize=1)
def _cmu() -> dict[str, list[list[str]]]:
    return _load_dict()


def is_vowel(phone: str) -> bool:
    return bool(VOWEL_RE.fullmatch(phone))


def strip_stress(phone: str) -> str:
    m = STRESS_RE.fullmatch(phone)
    return m.group(1) if m else phone


def stress_of(phone: str) -> int | None:
    m = STRESS_RE.fullmatch(phone)
    return int(m.group(2)) if m else None


def to_nonrhotic(phones: tuple[str, ...]) -> tuple[str, ...]:
    """Approximate southern British (RP) from CMUdict's American phones.

    Two transforms, both needed to grade British sources honestly:

    1. R-dropping. A coda R is not pronounced, so 'horse' (HH AO1 R S) and
       'sauce' (S AO1 S) become the same rime. Only post-vocalic R is dropped;
       onset R ('red', 'grace') is kept. ER0 becomes AH0 (schwa).
    2. BATH-broadening. AE before a voiceless fricative or nasal cluster
       becomes AA, so 'glass' matches 'arse' and 'banker' matches 'wanker'.

    This is deliberately approximate -- it is a rhyme-equivalence mapping, not
    a pronunciation model. It exists so that the non-rhotic rhyme-source
    assertions can be validated in the accent they were written for, instead of being scored as
    errors against American English.
    """
    out: list[str] = []
    for i, p in enumerate(phones):
        base = strip_stress(p)

        # ER -> schwa (non-rhotic 'butter', 'wanker')
        if base == "ER":
            st = stress_of(p)
            out.append(f"AH{st}" if st is not None else "AH")
            continue

        # drop post-vocalic R
        if base == "R" and out:
            prev = strip_stress(out[-1])
            if is_vowel(out[-1]) or prev in {"AH", "AA", "AO", "IH", "EH"}:
                nxt = strip_stress(phones[i + 1]) if i + 1 < len(phones) else ""
                if not nxt or not is_vowel(phones[i + 1]):
                    continue

        # BATH broadening: AE -> AA before a voiceless fricative / nasal
        if base == "AE" and i + 1 < len(phones):
            nxt = strip_stress(phones[i + 1])
            if nxt in _BATH_TRIGGERS:
                st = stress_of(p)
                out.append(f"AA{st}" if st is not None else "AA")
                continue

        # Before NG, CMUdict is inconsistent between AE and AA ('banker' AE
        # vs 'wanker' AA) though RP has the same vowel in both. Collapse to AE
        # so that contrast does not read as a rhyme failure.
        if base == "AA" and i + 1 < len(phones):
            if strip_stress(phones[i + 1]) == "NG":
                st = stress_of(p)
                out.append(f"AE{st}" if st is not None else "AE")
                continue

        out.append(p)
    return tuple(out)


@dataclass(frozen=True)
class Pron:
    """A single pronunciation, fully analysed."""

    word: str
    phones: tuple[str, ...]
    accent: str = "en-US"
    origin: str = "cmudict"

    @property
    def vowels(self) -> tuple[str, ...]:
        return tuple(p for p in self.phones if is_vowel(p))

    @property
    def syllables(self) -> int:
        return len(self.vowels)

    @property
    def stress_pattern(self) -> str:
        """e.g. 'beautiful' -> '100'. Secondary stress (2) kept distinct."""
        return "".join(str(stress_of(v)) for v in self.vowels)

    @property
    def primary_index(self) -> int:
        """Index into vowels of the FIRST primary-stressed syllable.

        Used for scansion/prosody, where onset stress matters.
        """
        for i, v in enumerate(self.vowels):
            if stress_of(v) == 1:
                return i
        return 0

    @property
    def anchor_index(self) -> int:
        """Index into vowels of the LAST STRESSED syllable (primary or secondary).

        This is the rhyme anchor. Anchoring on the last stress rather than the
        first is what lets phrases and compounds rhyme on their final stressed
        syllable -- otherwise 'apples and pears' would rhyme on 'apples'.

        Secondary stress (2) counts. CMUdict marks the rhyming syllable of a
        compound as secondary, not primary: 'sunflower' is S AH1 N F L AW2 ER0,
        so accepting only stress 1 anchored on 'sun' and built the rime from
        'N F L' -- the coda of 'sun' plus the ONSET of 'flower', a consonant run
        that straddles a syllable boundary and carries no rhyme information.
        That made 'sunflower' slant-match 'frustrating' (S T R), 'frustrated',
        'countryside' (N T R) and 'industrial' on a shared final liquid, while
        the true rhyme 'cauliflower' scored merely 'weak'. Including stress 2
        anchors on AW2 and yields the correct rime 'AW ER'.
        """
        last = 0
        for i, v in enumerate(self.vowels):
            if stress_of(v) in (1, 2):
                last = i
        return last

    @property
    def nucleus(self) -> str:
        """Stressed vowel without stress digit -- the assonance key."""
        vs = self.vowels
        if not vs:
            return ""
        return strip_stress(vs[self.anchor_index])

    @property
    def coda(self) -> str:
        """Trailing consonant cluster at word end -- the consonance key."""
        out: list[str] = []
        for p in reversed(self.phones):
            if is_vowel(p):
                break
            out.append(strip_stress(p))
        return " ".join(reversed(out))

    @property
    def tail_syllables(self) -> int:
        """Syllables after the rhyme anchor.

        Needed so slant comparison does not straddle different word shapes:
        'gravel' has 1 tail syllable, 'atmosphere' has 2, so their nucleus and
        coda are not positionally comparable even though both are stressed on
        the first vowel.
        """
        return max(self.syllables - self.anchor_index - 1, 0)
    @property
    def rhyme_ending(self) -> str:
        """Metrical landing: masculine (downbeat), feminine (double), or triple (dactylic)."""
        t = self.tail_syllables
        if t == 0:
            return "masculine"
        if t == 1:
            return "feminine"
        return "triple"


    @property
    def anchor_coda(self) -> str:
        """Consonants between the anchor vowel and the next vowel.

        For a word stressed on its final syllable this equals `coda`. For
        'gravel' (G R AE1 V AH0 L) it is 'V', not 'L' -- the consonant that
        actually closes the stressed syllable.
        """
        idxs = [i for i, p in enumerate(self.phones) if is_vowel(p)]
        if not idxs:
            return ""
        start = idxs[self.anchor_index] + 1
        out: list[str] = []
        for p in self.phones[start:]:
            if is_vowel(p):
                break
            out.append(strip_stress(p))
        return " ".join(out)

    def rime(self, depth: int = 1) -> str:
        """Rhyming part at N-syllable depth.

        depth=1 gives the classic rime (last stressed vowel onward is too
        loose for multisyllabic work, so we anchor on syllable boundaries).
        depth=2 spans the final two syllables, and so on. This is what makes
        multisyllabic rhyme matching possible.
        """
        idxs = [i for i, p in enumerate(self.phones) if is_vowel(p)]
        if not idxs:
            return ""
        take = min(depth, len(idxs))
        start = idxs[len(idxs) - take]
        return " ".join(strip_stress(p) for p in self.phones[start:])

    def rime_stressed(self) -> str:
        """Rime from the rhyme anchor onward (perfect-rhyme key).

        Anchors on the LAST primary stress, so phrases rhyme on their final
        stressed syllable rather than their first.
        """
        idxs = [i for i, p in enumerate(self.phones) if is_vowel(p)]
        if not idxs:
            return ""
        start = idxs[self.anchor_index]
        return " ".join(strip_stress(p) for p in self.phones[start:])

    @property
    def arpabet(self) -> str:
        return " ".join(self.phones)


@dataclass
class RhymeJudgement:
    kind: str           # perfect | identity | slant | assonance | consonance | none
    strength: float     # 0..1
    shared_syllables: int = 0
    detail: str = ""
    rhyme_class: str = ""   # perfect | family | additive_subtractive | assonance | consonance | none
    rhyme_ending: str = ""  # masculine | feminine | triple


def classify_rhyme(pa: Pron, pb: Pron, j_kind: str) -> str:
    """Classify into the 5 songwriting rhyme types:
    1. perfect: same vowel, same post-vocalic consonant(s)
    2. family: same vowel, consonant(s) from the same phonetic family
    3. additive_subtractive: same vowel, one side adds/removes consonant(s)
    4. assonance: same vowel, unrelated consonant(s)
    5. consonance: different vowel, same post-vocalic consonant(s)
    """
    if j_kind in ("perfect", "identity"):
        return "perfect"
    na, nb = pa.nucleus, pb.nucleus
    if not na or not nb:
        return "none"
    ca = pa.anchor_coda if pa.tail_syllables else pa.coda
    cb = pb.anchor_coda if pb.tail_syllables else pb.coda
    if na == nb:
        if ca == cb:
            return "perfect"
        if not ca or not cb:
            return "additive_subtractive"
        cas = ca.split()
        cbs = cb.split()
        if len(cas) != len(cbs):
            if ca.startswith(cb) or cb.startswith(ca) or ca.endswith(cb) or cb.endswith(ca):
                return "additive_subtractive"
        la, lb = cas[-1], cbs[-1]
        fa, fb = _CONSONANT_FAMILY.get(la), _CONSONANT_FAMILY.get(lb)
        if fa and fa == fb:
            return "family"
        return "assonance"
    if ca and ca == cb:
        return "consonance"
    return "none"

class Phonetics:
    """Lookup + comparison over CMUdict."""

    def __init__(self, accent: str = "en-US") -> None:
        self.accent = accent
        self._d = _cmu()
        self._nonrhotic = accent == "en-GB-nonrhotic"

    # -- lookup -------------------------------------------------------
    def prons(self, word: str) -> list[Pron]:
        key = word.strip().lower()
        raw = self._d.get(key)
        if not raw:
            # Morphological fallback for derived words missing from CMUdict
            for suffix, s_phones in _PRODUCTIVE_SUFFIXES:
                if key.endswith(suffix) and len(key) > len(suffix) + 2:
                    stem = key[:-len(suffix)]
                    # check stem, stem+e (e.g. jaded->jade), or un-doubled consonant
                    candidates = [stem]
                    if not stem.endswith("e"):
                        candidates.append(stem + "e")
                    if len(stem) > 2 and stem[-1] == stem[-2]:
                        candidates.append(stem[:-1])
                    for st in candidates:
                        if st in self._d:
                            st_prons = self._d[st]
                            if self._nonrhotic:
                                return [Pron(key, to_nonrhotic(tuple(ph) + s_phones), self.accent,
                                             origin="derived-nonrhotic") for ph in st_prons]
                            return [Pron(key, tuple(ph) + s_phones, self.accent, origin="derived") for ph in st_prons]
            # Check DuckDB lexicon if available
            try:
                rdb = getattr(self, "_rdb", None)
                if rdb is None and not getattr(self, "_rdb_failed", False):
                    try:
                        from songwriter.lexicon.query import RhymeDB
                        self._rdb = rdb = RhymeDB()
                    except Exception:
                        self._rdb_failed = True
                if rdb is not None:
                    info = rdb.info(key)
                    if info and info.get("pronunciations"):
                        r_prons = []
                        for p in info["pronunciations"]:
                            phones = tuple(p["arpabet"].split())
                            if self._nonrhotic:
                                r_prons.append(Pron(key, to_nonrhotic(phones), self.accent, origin="rhymedb-nonrhotic"))
                            else:
                                r_prons.append(Pron(key, phones, self.accent, origin="rhymedb"))
                        if r_prons:
                            return r_prons
            except Exception:
                pass

            # Compound word decomposer fallback (e.g. "snowbear" -> "snow" + "bear")
            for i in range(3, len(key) - 2):
                p1 = self._d.get(key[:i])
                p2 = self._d.get(key[i:])
                if p1 and p2:
                    # Primary stress on part 1, secondary on part 2
                    p2_sec = tuple(ph.replace("1", "2") if ph.endswith("1") else ph for ph in p2[0])
                    comp_phones = tuple(p1[0]) + p2_sec
                    if self._nonrhotic:
                        return [Pron(key, to_nonrhotic(comp_phones), self.accent, origin="compound-nonrhotic")]
                    return [Pron(key, comp_phones, self.accent, origin="compound")]

            # Colloquial G-dropping suffix fallback (e.g. "runnin" / "chillin" -> "running" / "chilling")
            if (key.endswith("in") and len(key) > 4) or (key.endswith("in'") and len(key) > 4):
                stem = key[:-3] if key.endswith("in'") else key[:-2]
                base_ing = stem + "ing"
                raw_ing = self._d.get(base_ing)
                if raw_ing:
                    mod_prons = []
                    for p_tuple in raw_ing:
                        ph_list = list(p_tuple)
                        if ph_list and ph_list[-1] == "NG":
                            ph_list[-1] = "N"
                        phones = tuple(ph_list)
                        if self._nonrhotic:
                            mod_prons.append(Pron(key, to_nonrhotic(phones), self.accent, origin="colloquial-nonrhotic"))
                        else:
                            mod_prons.append(Pron(key, phones, self.accent, origin="colloquial"))
                    if mod_prons:
                        return mod_prons
            return []
        if self._nonrhotic:
            return [Pron(key, to_nonrhotic(tuple(ph)), self.accent,
                         origin="cmudict-nonrhotic") for ph in raw]
        return [Pron(key, tuple(ph), self.accent) for ph in raw]

    def best(self, word: str) -> Pron | None:
        ps = self.prons(word)
        return ps[0] if ps else None

    def known(self, word: str) -> bool:
        return word.strip().lower() in self._d

    def syllables(self, word: str) -> int | None:
        p = self.best(word)
        if p:
            return p.syllables
        if "-" in word:
            parts = [pt for pt in word.split("-") if pt]
            return sum(self.syllables(pt) or 1 for pt in parts)
        return estimate_syllables(word)

    # -- phrases ------------------------------------------------------
    def phrase_prons(self, phrase: str) -> Pron | None:
        """Concatenate word pronunciations so multi-word phrases can rhyme.

        The slang rhyme source asserts thousands of phrase->word rhymes, which CMUdict
        alone cannot represent. Returns None if any token is out of vocab.
        """
        toks = [t for t in re.split(r"[^a-z']+", phrase.lower()) if t]
        if not toks:
            return None
        phones: list[str] = []
        for t in toks:
            p = self.best(t)
            if p is None:
                return None
            phones.extend(p.phones)
        return Pron(phrase.lower(), tuple(phones), self.accent, origin="composed")
    def mosaic_prons(self, phrase: str) -> Pron | None:
        """Compose pronunciation for a multi-word phrase with connected speech weak forms."""
        toks = [t for t in re.split(r"[^a-z']+", phrase.lower()) if t]
        if not toks:
            return None
        phones: list[str] = []
        for i, t in enumerate(toks):
            if t in WEAK_FORMS and (len(toks) > 1):
                phones.extend(WEAK_FORMS[t])
            else:
                p = self.best(t)
                if p is None:
                    return None
                phones.extend(p.phones)
        return Pron(phrase.lower(), tuple(phones), self.accent, origin="mosaic")


    def resolve(self, text: str) -> Pron | None:
        """Word if known, else phrase composition."""
        p = self.best(text)
        if p:
            return p
        return self.phrase_prons(text)

    # -- comparison ---------------------------------------------------
    def compare(self, a: str, b: str) -> RhymeJudgement:
        pas, pbs = self.prons(a), self.prons(b)
        if not pas or not pbs:
            pa, pb = self.resolve(a), self.resolve(b)
            if not pa or not pb:
                return RhymeJudgement("none", 0.0, detail="out_of_vocab")
            return self.compare_prons(pa, pb)
        # Evaluate across all available pronunciations (e.g. Lead verb /li:d/ vs lead metal /lɛd/)
        best_j = None
        for pa in pas:
            for pb in pbs:
                j = self.compare_prons(pa, pb)
                if best_j is None or j.strength > best_j.strength:
                    best_j = j
        return best_j or RhymeJudgement("none", 0.0, detail="out_of_vocab")
    def _decorate(self, pa: Pron, pb: Pron, j: RhymeJudgement) -> RhymeJudgement:
        j.rhyme_ending = pa.rhyme_ending if pa.rhyme_ending == pb.rhyme_ending else f"{pa.rhyme_ending}/{pb.rhyme_ending}"
        j.rhyme_class = classify_rhyme(pa, pb, j.kind)
        return j

    def compare_prons(self, pa: Pron, pb: Pron) -> RhymeJudgement:
        if pa.phones == pb.phones:
            return self._decorate(pa, pb, RhymeJudgement("identity", 1.0, pa.syllables, "same pronunciation"))

        # Deepest syllable-aligned matching rime, for multisyllabic ranking.
        depth = 0
        max_depth = min(pa.syllables, pb.syllables)
        for d in range(1, max_depth + 1):
            if pa.rime(d) and pa.rime(d) == pb.rime(d):
                depth = d
            else:
                break

        # PERFECT rhyme is defined from the PRIMARY-STRESSED vowel onward.
        # Without this anchor, 'happy'/'city' would score as perfect just
        # because both end in an unstressed IY.
        ra, rb = pa.rime_stressed(), pb.rime_stressed()
        if ra and ra == rb:
            strength = min(1.0, 0.8 + 0.07 * max(depth - 1, 0))
            return self._decorate(pa, pb, RhymeJudgement("perfect", strength, max(depth, 1),
                                  f"shared stressed rime, depth {max(depth, 1)}"))

        if depth >= 1:
            # Tails match but the stressed vowel differs: a weak / family
            # rhyme (common in modern pop, but not a true perfect rhyme).
            return self._decorate(pa, pb, RhymeJudgement("weak", min(0.55, 0.3 + 0.12 * depth), depth,
                                  f"unstressed tail match, depth {depth}"))

        na, nb = pa.nucleus, pb.nucleus

        # Slant/assonance comparison must be positionally comparable. If the
        # two words carry a different number of syllables AFTER the rhyme
        # anchor, their nucleus and coda sit in different places and any match
        # is an artifact. 'gravel' (1 tail syllable) vs 'atmosphere' (2) was
        # scoring as slant by pairing AE+L against AE+R across a 3-syllable
        # gap. Word-final codas are only comparable for anchor-final words.
        if pa.tail_syllables != pb.tail_syllables:
            return self._decorate(pa, pb, RhymeJudgement("none", 0.0, 0, "different word shape"))

        # Use the consonants closing the ANCHOR syllable, not the word end.
        ca = pa.anchor_coda if pa.tail_syllables else pa.coda
        cb = pb.anchor_coda if pb.tail_syllables else pb.coda

        # Slant: same nucleus, related-but-different coda.
        if na and na == nb and ca != cb:
            if self._coda_related(ca, cb):
                return self._decorate(pa, pb, RhymeJudgement("slant", 0.62, 1, "same vowel, related coda"))
            return self._decorate(pa, pb, RhymeJudgement("assonance", 0.45, 1, "same vowel, different coda"))

        # Slant: neighbouring vowel, identical coda.
        if ca and ca == cb and nb in _VOWEL_NEIGHBOURS.get(na, set()):
            return self._decorate(pa, pb, RhymeJudgement("slant", 0.58, 1, "near vowel, same coda"))

        if ca and ca == cb:
            return self._decorate(pa, pb, RhymeJudgement("consonance", 0.4, 1, "shared coda"))

        if na and nb in _VOWEL_NEIGHBOURS.get(na, set()):
            return self._decorate(pa, pb, RhymeJudgement("assonance", 0.32, 1, "near vowel"))

        return self._decorate(pa, pb, RhymeJudgement("none", 0.0, 0))

    @staticmethod
    def _coda_related(ca: str, cb: str) -> bool:
        if not ca or not cb:
            return False
        la, lb = ca.split()[-1], cb.split()[-1]
        return _CONS_CLASS.get(la) == _CONS_CLASS.get(lb) and la != lb

    # -- search -------------------------------------------------------
    def rhymes(self, word: str, kinds: tuple[str, ...] = ("perfect",),
               min_depth: int = 1, limit: int = 50) -> list[tuple[str, RhymeJudgement]]:
        """Scan vocabulary for rhymes. Deliberately capped: returning huge
        candidate lists to an LLM recreates context flooding.
        """
        src_list = self.prons(word)
        if not src_list:
            p_res = self.resolve(word)
            if p_res:
                src_list = [p_res]
        if not src_list:
            return []

        out: list[tuple[str, RhymeJudgement]] = []
        seen: set[str] = {word.lower()}

        # 1. Direct search against vocabulary
        targets = {s.rime(min_depth) for s in src_list if s.rime(min_depth)}
        for cand in self._d:
            if cand in seen:
                continue
            for ph in self._d[cand]:
                p = Pron(cand, tuple(ph), self.accent)
                best_j: RhymeJudgement | None = None
                for s in src_list:
                    j = self.compare_prons(s, p)
                    if j.kind in kinds and j.shared_syllables >= min_depth:
                        if best_j is None or j.strength > best_j.strength:
                            best_j = j
                if best_j:
                    out.append((cand, best_j))
                    seen.add(cand)
                    break

        # 2. Suffix decomposition fallback if needed
        key = word.strip().lower()
        for suffix, s_phones in _PRODUCTIVE_SUFFIXES:
            if key.endswith(suffix) and len(key) > len(suffix) + 2:
                stem = key[:-len(suffix)]
                stem_rhymes = self.rhymes(stem, kinds=kinds, min_depth=min_depth, limit=limit)
                s_syl = sum(1 for ph in s_phones if VOWEL_RE.search(ph))
                for stem_cand, stem_j in stem_rhymes:
                    derived_cand = stem_cand + suffix
                    if derived_cand not in seen:
                        derived_j = RhymeJudgement(
                            kind=stem_j.kind,
                            strength=stem_j.strength,
                            shared_syllables=stem_j.shared_syllables + s_syl,
                            detail=f"derived via -{suffix}: {stem_j.detail}"
                        )
                        out.append((derived_cand, derived_j))
                        seen.add(derived_cand)
                break

        # Tiebreak on syllable count before spelling. This layer has no corpus
        # frequency (that lives in the service), so word length is the only
        # available proxy for singability -- and a purely alphabetical tiebreak
        # fills the cap with 'acolyte', 'albright', 'allbright' and buries
        # 'night' and 'bright' behind them.
        syl_of = {c: (self.best(c).syllables if self.best(c) else 99) for c, _ in out}
        out.sort(key=lambda x: (-x[1].shared_syllables, -x[1].strength,
                                syl_of.get(x[0], 99), x[0]))
        return out[:limit]


@dataclass
class LineScan:
    """Scansion of a single line."""

    text: str
    words: list[str] = field(default_factory=list)
    syllables: int = 0
    stress_grid: str = ""
    unknown: list[str] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return not self.unknown


def scan_line(text: str, ph: Phonetics) -> LineScan:
    """Map a line to a stress grid. Unknown words are reported, not guessed."""
    toks = [t for t in re.split(r"[^A-Za-z']+", text) if t]
    scan = LineScan(text=text, words=toks)
    grid: list[str] = []
    for t in toks:
        p = ph.best(t)
        if p is None:
            scan.unknown.append(t)
            continue
        pat = p.stress_pattern
        # Monosyllables are contextually stressed; mark as ambiguous '1'
        grid.append(pat if pat else "")
    scan.stress_grid = "".join(grid)
    scan.syllables = len(scan.stress_grid)
    return scan
