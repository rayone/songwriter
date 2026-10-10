"""IPA to ARPAbet conversion, for lexicons that give IPA rather than phones.

CMUdict stops at ~126k words and has no modern slang: no 'shawty', 'homie',
'dawg', 'hella', 'rizz'. Wiktionary (via the Kaikki extract) has all of them
*with* IPA, so converting IPA to ARPAbet is what unlocks that vocabulary.

Accuracy is measurable rather than assumed. For the ~33k words appearing in
both Kaikki and CMUdict, CMUdict is ground truth, so the conversion can be
scored directly -- and accuracy there is the estimate for slang, where no
ground truth exists. See ``scripts/measure_ipa.py``.

Wiktionary IPA is not a single notation. It mixes narrow and broad
transcription, several dialects, and competing conventions for the same sound:
'pie' appears as /paɪ/ and /pɑɪ/, 'free' as /fɹi/ and /fɹɪj/, 'brown' with
/aʊ/ and /æʊ̯/. Each of those variants needs to reach one ARPAbet rime, which
is what most of the mapping table is for.
"""

from __future__ import annotations

import unicodedata

#: ARPAbet vowels. Everything else is a consonant.
ARPABET_VOWELS = frozenset({
    "AA", "AE", "AH", "AO", "AW", "AY", "EH", "ER", "EY",
    "IH", "IY", "OW", "OY", "UH", "UW",
})

# -- characters to discard --------------------------------------------

#: Delimiters, boundaries and diacritics that carry no ARPAbet distinction.
#: ARPAbet has no aspiration, nasalisation, dentalisation or devoicing, so
#: keeping these would only prevent the base symbol from matching.
_DROP = frozenset(
    "/[]()"          # transcription delimiters
    "\u02c0"         # ejective
    "\u02b0"         # ʰ aspiration
    "\u02b7"         # ʷ labialisation
    "\u02b2"         # ʲ palatalisation
    "\u02de"         # ˞ rhotic hook (handled by ɚ/ɝ mappings)
    "\u02d1\u02d0"   # ˑ ː length
    "\u02d4\u02d5"   # ˔ ˕ raising/lowering
    "\u0303"         # ̃ nasalisation
    "\u0308"         # ̈ centralisation
    "\u031a"         # ̚ unreleased
    "\u031d\u031e"   # ̝ ̞ raised/lowered
    "\u031f\u0320"   # ̟ ̠ advanced/retracted
    "\u0325\u030a"   # ̥ ̊ voiceless
    "\u032a"         # ̪ dental
    "\u032f"         # ̯ non-syllabic (diphthongs handled as digraphs)
    "\u0334"         # ̴ velarised
    "\u033a\u033b"   # ̺ ̻ apical/laminal
    "\u0361\u035c"   # ͡ ͜ tie bars (affricates handled as digraphs)
    "\u200d\u200b"   # zero-width joiners
    "\u203f"         # ‿ undertie
    "~-\u2011 \t"    # linking, hyphens, whitespace
    "\u02bc'\u2019"  # apostrophes
    "\u0294"         # ʔ glottal stop: no ARPAbet equivalent
    "*!?,;:"         # stray punctuation
)

#: Primary and secondary stress marks.
_PRIMARY = "\u02c8"      # ˈ
_SECONDARY = "\u02cc"    # ˌ
#: Wiktionary marks syllable divisions with '.', which we track separately
#: because it is an independent check on our own syllabifier.
_SYLLABLE_BREAK = "."

# -- the mapping table -------------------------------------------------
#
# Ordered longest-first: the converter tries each pattern at the current
# position and takes the first match, so 'tʃ' must precede 't'.

_MAP: tuple[tuple[str, str], ...] = (
    # --- affricates (before their component stops) ---
    ("t\u0283", "CH"),      # tʃ
    ("d\u0292", "JH"),      # dʒ
    ("t\u0255", "CH"),      # tɕ
    ("d\u0291", "JH"),      # dʑ
    ("ts", "T S"),
    ("dz", "D Z"),

    # --- syllabic consonants ---
    # Wiktionary writes 'abduction' as /æbˈdʌk.ʃn̩/ and 'abnormal' as
    # /ˌæbˈnɔɹ.ml̩/, marking the consonant itself as the syllable nucleus.
    # CMUdict spells the same syllable with an explicit reduced vowel
    # (AH0 N, AH0 L), so the mark has to become a schwa. Discarding it as a
    # diacritic produced vowel-less syllables -- 'SH N' for -tion -- and was
    # the single largest source of rime errors.
    ("l\u0329", "AH L"),
    ("n\u0329", "AH N"),
    ("m\u0329", "AH M"),
    ("\u026b\u0329", "AH L"),        # ɫ̩
    ("\u026c\u0329", "AH L"),        # ɬ̩
    ("\u0279\u0329", "ER"),          # ɹ̩
    ("\u027b\u0329", "ER"),          # ɻ̩
    ("r\u0329", "ER"),
    ("\u014b\u0329", "AH NG"),       # ŋ̩
    ("\u0271\u0329", "AH M"),        # ɱ̩
    ("\u0283\u0329", "AH SH"),       # ʃ̩
    ("s\u0329", "AH S"),
    ("z\u0329", "AH Z"),
    ("d\u0329", "AH D"),
    ("t\u0329", "AH T"),
    ("\u028d\u0329", "AH W"),
    ("\u02e1\u0329", "AH L"),

    # --- r-coloured vowels (before plain vowels and r) ---
    ("\u025a", "ER"),               # ɚ
    ("\u025d", "ER"),               # ɝ
    ("\u025c\u0279", "ER"),         # ɜɹ
    ("\u025c\u027b", "ER"),         # ɜɻ
    ("\u025cr", "ER"),
    ("\u0259\u0279", "ER"),         # əɹ
    ("\u0259\u027b", "ER"),
    ("\u028c\u0279", "ER"),         # ʌɹ
    ("\u025e\u0279", "ER"),

    # --- diphthongs, and the variant spellings of each ---
    # PRICE: aɪ ~ ɑɪ ~ ʌɪ ~ æɪ ~ aj
    ("a\u026a", "AY"), ("\u0251\u026a", "AY"), ("\u028c\u026a", "AY"),
    ("a\u0269", "AY"), ("aj", "AY"), ("\u0251j", "AY"),
    # MOUTH: aʊ ~ æʊ ~ ɑʊ ~ ʌʊ ~ aw
    ("a\u028a", "AW"), ("\u00e6\u028a", "AW"), ("\u0251\u028a", "AW"),
    ("\u028c\u028a", "AW"), ("aw", "AW"), ("\u00e6w", "AW"),
    # CHOICE: ɔɪ ~ oɪ ~ ɒɪ ~ ɔj
    ("\u0254\u026a", "OY"), ("o\u026a", "OY"), ("\u0252\u026a", "OY"),
    ("\u0254j", "OY"),
    # FACE: eɪ ~ æɪ ~ ɛɪ ~ ej ~ ɛj
    ("e\u026a", "EY"), ("\u00e6\u026a", "EY"), ("\u025b\u026a", "EY"),
    ("ej", "EY"), ("\u025bj", "EY"), ("e\u0259", "EY"),
    # GOAT: oʊ ~ əʊ ~ ɔʊ ~ ow
    ("o\u028a", "OW"), ("\u0259\u028a", "OW"), ("\u0254\u028a", "OW"),
    ("ow", "OW"), ("\u0264\u028a", "OW"),
    # FLEECE: iː ~ ɪj ~ ij  (the j-glide spelling is common in Wiktionary)
    ("\u026aj", "IY"), ("ij", "IY"), ("i\u02d0", "IY"),
    # GOOSE: uː ~ ʊw ~ uw ~ ʉw
    ("\u028aw", "UW"), ("uw", "UW"), ("\u0289w", "UW"), ("\u0289", "UW"),

    # --- consonant digraphs ---
    ("\u014b", "NG"),   # ŋ
    ("\u03b8", "TH"),   # θ
    ("\u00f0", "DH"),   # ð
    ("\u0283", "SH"),   # ʃ
    ("\u0292", "ZH"),   # ʒ
    ("\u0255", "SH"),   # ɕ
    ("\u0291", "ZH"),   # ʑ

    # --- monophthongs ---
    ("\u026a", "IH"),   # ɪ
    ("i", "IY"),
    ("\u025b", "EH"),   # ɛ
    ("e", "EY"),        # bare e is FACE in most English transcription
    ("\u00e6", "AE"),   # æ
    ("a", "AE"),        # bare a: TRAP in American-leaning transcription
    ("\u00e4", "AA"),   # ä
    ("\u028c", "AH"),   # ʌ
    ("\u0259", "AH"),   # ə
    ("\u0250", "AH"),   # ɐ
    ("\u0258", "AH"),   # ɘ
    ("\u0275", "AH"),   # ɵ
    ("\u0251", "AA"),   # ɑ
    ("\u0252", "AA"),   # ɒ  (father-bother merger)
    ("\u0254", "AO"),   # ɔ
    ("o", "OW"),
    ("\u028a", "UH"),   # ʊ
    ("u", "UW"),
    ("\u0268", "IH"),   # ɨ
    ("\u00f8", "ER"),   # ø
    ("\u025c", "ER"),   # ɜ
    ("\u025e", "ER"),   # ɞ
    ("y", "IY"),        # rare in English IPA

    # --- single consonants ---
    ("p", "P"), ("b", "B"), ("t", "T"), ("d", "D"), ("k", "K"), ("g", "G"),
    ("\u0261", "G"),                                   # ɡ script g
    ("f", "F"), ("v", "V"), ("s", "S"), ("z", "Z"),
    ("m", "M"), ("n", "N"), ("l", "L"),
    ("\u026b", "L"), ("\u026c", "L"), ("\u02e1", "L"),  # ɫ ɬ ˡ
    ("\u0279", "R"), ("r", "R"), ("\u027b", "R"),       # ɹ r ɻ
    ("\u0281", "R"), ("\u0280", "R"),                   # ʁ ʀ
    ("\u027e", "T"),                                    # ɾ flap ('better')
    ("\u0288", "T"), ("\u0256", "D"),                   # ʈ ɖ retroflex
    ("w", "W"), ("\u028d", "W"), ("\u0265", "W"),       # w ʍ ɥ
    ("j", "Y"),
    ("h", "HH"), ("\u0266", "HH"), ("x", "HH"),         # h ɦ x
    ("\u00e7", "HH"), ("\u0271", "M"),                  # ç ɱ
    ("\u0273", "N"), ("\u0272", "N"), ("\u014b", "NG"), # ɳ ɲ
    ("\u028b", "V"),                                    # ʋ
    ("\u03c7", "HH"),                                   # χ
)

#: Dialect tags that mean a transcription is *not* General American. Kaikki
#: attaches these per-pronunciation, so they let us prefer US variants when
#: converting for an American target accent.
NON_US_TAGS = frozenset({
    "Received-Pronunciation", "RP", "UK", "British", "England",
    "Australia", "Australian", "New-Zealand", "General-Australian",
    "Ireland", "Irish", "Scotland", "Scottish", "Wales", "Welsh",
    "South-Africa", "India", "South-Asia", "Nigeria", "Jamaica",
    "Canada",   # close to US but has distinctive raising
})

US_TAGS = frozenset({
    "US", "General-American", "GA", "American", "GenAm",
    "Mid-Atlantic", "cot-caught-merger", "California",
})


class IPAConversionError(ValueError):
    """Raised when an IPA string contains symbols outside the mapping."""


#: Separators Wiktionary uses between ALTERNATIVE transcriptions inside one
#: string: '[ˈflaʊ̯.ɚ] ~ [ˈflaʊ̯.ɹ̩]' is two pronunciations of 'flower', not one.
#: Treating the string as a unit concatenated them, giving a four-syllable
#: 'F L AW1 ER0 F L AW1 ER0'.
_ALTERNATIVE_SEPARATORS = ("~", ",", " or ", ";", "]/[")


def split_alternatives(ipa: str) -> list[str]:
    """Split one Wiktionary IPA field into its separate transcriptions."""
    parts = [ipa]
    for separator in _ALTERNATIVE_SEPARATORS:
        parts = [piece for part in parts for piece in part.split(separator)]
    return [p.strip() for p in parts if p.strip(" /[]()")]


def normalise(ipa: str) -> str:
    """Strip delimiters and diacritics that carry no ARPAbet distinction.

    Applies NFD first so that precomposed characters decompose into a base
    plus combining marks, letting the marks be dropped individually.
    """
    text = unicodedata.normalize("NFD", ipa.strip())
    return "".join(ch for ch in text if ch not in _DROP)


def ipa_to_arpabet(ipa: str, *, strict: bool = False) -> tuple[str, ...] | None:
    """Convert an IPA string to stressed ARPAbet phones.

    Returns ``None`` when the string contains symbols outside the mapping, or
    raises :class:`IPAConversionError` when ``strict``. Refusing to guess is
    deliberate: a wrong pronunciation is worse than a missing one, because it
    produces confident false rhymes.

    Stress follows IPA convention, where the mark precedes its syllable:
    ``ˈ`` gives 1, ``ˌ`` gives 2, unmarked gives 0. When a string carries no
    mark at all, the first vowel takes primary stress -- true of every
    monosyllable and the best available guess otherwise.

    >>> ipa_to_arpabet("/ˈʃɔːti/")
    ('SH', 'AO1', 'T', 'IY0')
    """
    # One Wiktionary field can hold several alternative transcriptions
    # ('[ˈflaʊ̯.ɚ] ~ [ˈflaʊ̯.ɹ̩]'). Convert the first; concatenating them gave a
    # four-syllable 'flower'.
    alternatives = split_alternatives(ipa)
    text = normalise(alternatives[0] if alternatives else ipa)
    if not text:
        return None

    has_explicit_stress = _PRIMARY in text or _SECONDARY in text
    phones: list[str] = []
    pending_stress: int | None = None
    seen_vowel = False
    i = 0
    # Index into `phones` of each vowel, so trailing full vowels can be
    # promoted to secondary stress once the whole word is known.
    vowel_positions: list[int] = []

    while i < len(text):
        ch = text[i]

        if ch == _PRIMARY:
            pending_stress = 1
            i += 1
            continue
        if ch == _SECONDARY:
            pending_stress = 2
            i += 1
            continue
        if ch == _SYLLABLE_BREAK:
            i += 1
            continue

        for pattern, arpa in _MAP:
            if not text.startswith(pattern, i):
                continue
            for symbol in arpa.split():
                if symbol in ARPABET_VOWELS:
                    if pending_stress is not None:
                        stress = pending_stress
                    elif has_explicit_stress:
                        stress = 0
                    else:
                        stress = 0 if seen_vowel else 1
                    vowel_positions.append(len(phones))
                    phones.append(f"{symbol}{stress}")
                    pending_stress = None
                    seen_vowel = True
                else:
                    # ER already carries its r-colouring, so a following rhotic
                    # is the same segment written twice ('word' as /wɜɹd/ plus
                    # the ɜɹ digraph would otherwise give ER R D).
                    if symbol == "R" and phones and phones[-1][:2] == "ER":
                        continue
                    phones.append(symbol)
            i += len(pattern)
            break
        else:
            if strict:
                raise IPAConversionError(
                    f"unmapped {text[i]!r} (U+{ord(text[i]):04X}) in {ipa!r}")
            return None

    if not phones or not seen_vowel:
        return None
    _promote_trailing_full_vowels(phones, vowel_positions)
    return tuple(phones)


#: Vowels that are never the product of reduction. A schwa or reduced IH in a
#: final syllable really is unstressed; an EY, AY or OW is not.
#:
#: IY is deliberately absent. Word-final unstressed IY is the "happY" vowel,
#: which English reduces despite its full quality, and CMUdict marks it IY0
#: ('city' S IH1 T IY0, 'committee' K AH0 M IH1 T IY0). Promoting it would move
#: the rhyme anchor onto the final syllable and make 'happy' and 'city' perfect
#: rhymes -- the very error the stress anchor exists to prevent.
_FULL_VOWELS = frozenset({
    "AA", "AE", "AO", "AW", "AY", "EH", "EY", "OW", "OY", "UW",
})


def _promote_trailing_full_vowels(phones: list[str],
                                  vowel_positions: list[int]) -> None:
    """Mark unreduced vowels after the primary stress as secondary stress.

    IPA marks only primary and (sometimes) secondary stress, leaving the rest
    bare. CMUdict instead assigns secondary stress to any vowel that keeps its
    full quality: 'fortnight' is F AO1 R T N AY2 T, not AY0, because the second
    syllable is unreduced.

    This matters because the rhyme anchor is the last vowel carrying stress 1
    or 2. Leaving a full final vowel at 0 moves the anchor back to the primary
    stress and builds the rime from the wrong syllable -- so 'fortnight' would
    rhyme on 'fort' rather than 'night'. It is the same failure that made
    'sunflower' rhyme on 'sun'.

    Reduced vowels (AH, IH, UH, ER) are left alone: those genuinely are
    unstressed, and promoting them would move the anchor too far forward.
    """
    if len(vowel_positions) < 2:
        return
    primary = next((i for i in vowel_positions
                    if phones[i].endswith("1")), None)
    if primary is None:
        return
    for index in vowel_positions:
        if index <= primary:
            continue
        if not phones[index].endswith("0"):
            continue
        if phones[index][:-1] in _FULL_VOWELS:
            phones[index] = f"{phones[index][:-1]}2"


def syllable_breaks(ipa: str) -> int:
    """Number of syllables Wiktionary marks in ``ipa``, or 0 if unmarked.

    Wiktionary hand-marks divisions with '.', which makes this an independent
    check on :mod:`songwriter.phonetics.syllabify` rather than a second guess
    from the same algorithm.
    """
    text = normalise(ipa)
    if _SYLLABLE_BREAK not in text:
        return 0
    return text.count(_SYLLABLE_BREAK) + 1


def prefer_us(sounds: list[dict]) -> list[str]:
    """IPA strings from a Kaikki ``sounds`` list, American variants first.

    Kaikki tags pronunciations by dialect. Ordering matters because the target
    accent is General American: an RP transcription of 'bath' or 'father'
    converts to a different ARPAbet rime and would look like a conversion
    error when it is really a dialect difference.
    """
    us: list[str] = []
    untagged: list[str] = []
    other: list[str] = []
    for sound in sounds:
        ipa = sound.get("ipa")
        if not ipa:
            continue
        tags = set(sound.get("tags") or ())
        if tags & US_TAGS:
            us.append(ipa)
        elif tags & NON_US_TAGS:
            other.append(ipa)
        else:
            untagged.append(ipa)
    return us + untagged + other
