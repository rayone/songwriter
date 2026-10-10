"""Craft diagnostics: deterministic metrics over lines and sections.

Design premise: a small model has no phonetic intuition and cannot reliably
count syllables or hear a rhyme. So this layer computes the verdict and the
model only has to act on it. Every function here returns numbers or short
labels -- never prose, never a candidate dump.
"""

from __future__ import annotations
from functools import lru_cache
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from songwriter.db import Database

from songwriter.phonetics import Phonetics

from songwriter.config import CORPUS_DB as DEFAULT_DB
_TOKEN = re.compile(r"[a-z']+")

# Non-word vocal tags ("ah", "ayy", "yeah") that sit after the lyric. They are
# delivery, not the rhyme word: the end word is the last word before them.
VOCABLE_TAGS = frozenset({
    "ah", "aah", "ahh", "oh", "ooh", "oo", "yeah", "yea", "yah", "uh", "huh", "hey",
    "woah", "whoa", "wo", "ayy", "ay", "na", "la", "mmm", "hmm", "ha", "eh", "yo",
    "woo", "ow", "da", "ba",
})
# Abstractions LLMs reach for instead of concrete images. Weighted by how
# reliably they signal telling-not-showing.
ABSTRACT_WORDS = {
    "love", "heart", "soul", "pain", "hope", "dream", "dreams", "fear",
    "forever", "eternity", "destiny", "fate", "freedom", "truth", "lies",
    "beauty", "sorrow", "joy", "misery", "loneliness", "emptiness", "desire",
    "passion", "memories", "feelings", "emotion", "happiness", "sadness",
    "darkness", "light", "shadows", "angel", "heaven", "hell", "paradise",
    "infinity", "eternal", "endless", "forsaken", "broken", "shattered",
}

# Concrete sensory anchors: physical nouns and verbs that put a listener
# somewhere. Not exhaustive -- used as a ratio, not a gate.
SENSORY_WORDS = {
    "car", "door", "street", "kitchen", "window", "cigarette", "coffee",
    "phone", "bed", "floor", "wall", "rain", "snow", "dust", "glass",
    "keys", "coat", "shoes", "hands", "teeth", "hair", "skin", "knuckles",
    "engine", "radio", "porch", "gravel", "curtain", "sink", "bottle",
    "match", "smoke", "asphalt", "diner", "highway", "parking", "stairs",
    "elevator", "screen", "receipt", "grocery", "sidewalk", "fence",
}

# Weak function words ("traffic directors") that must not anchor a rhyming couplet.
# Rhyme anchors must be content words: nouns, principal verbs, adjectives.
TRAFFIC_DIRECTOR_WORDS = frozenset({
    # Prepositions
    "to", "of", "in", "for", "on", "with", "at", "by", "from", "up", "about", "into", "over", "after", "through",
    # Articles
    "the", "a", "an",
    # Conjunctions
    "and", "but", "or", "nor", "so", "as", "if", "than", "because", "though", "while", "until",
    # Auxiliary verbs & copulas
    "is", "am", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "would", "could", "should", "might", "must", "will", "shall",
    # Weak pronouns & possessive determiners
    "it", "its", "my", "your", "his", "her", "their", "our", "this", "that", "these", "those",
})

COMMON_INVERTIBLE_NOUNS = frozenset({
    "heart", "hearts", "night", "nights", "day", "days", "eyes", "eye", "tears", "tear",
    "soul", "souls", "face", "faces", "hand", "hands", "voice", "voices", "room", "rooms",
    "street", "streets", "sky", "skies", "world", "worlds", "words", "word", "mind", "minds",
    "shadow", "shadows", "light", "lights", "sun", "moon", "star", "stars", "wind", "winds",
    "water", "waters", "blood", "fire", "dream", "dreams", "road", "roads",
})

COMMON_POSTPOSITIVE_ADJECTIVES = frozenset({
    "dark", "bright", "cold", "warm", "true", "deep", "blue", "grey", "gray", "red", "green",
    "black", "white", "blind", "clear", "dear", "rare", "bare", "fair", "pure", "sure",
    "broken", "spoken", "unspoken", "frozen", "lonely", "bold", "old", "young", "new",
    "sweet", "sour", "strong", "weak", "dead", "alive", "alone", "wild", "still",
})


def detect_poetic_inversion(text: str) -> str | None:
    """Detect unnatural syntactic inversion forced for rhyme.

    Flags postpositive adjective inversion (e.g. 'heart broken', 'night dark')
    and archaic declarative verb inversions (e.g. 'walked I', 'saw we').
    """
    words = re.findall(r"[a-z']+", text.lower())
    if len(words) < 2:
        return None
    w_prev, w_end = words[-2], words[-1]
    if w_prev in COMMON_INVERTIBLE_NOUNS and w_end in COMMON_POSTPOSITIVE_ADJECTIVES:
        return f"{w_prev} {w_end}"
    if w_end in ("i", "he", "she", "we", "they") and w_prev.endswith(("ed", "elt", "ent", "aid")):
        if w_prev not in ("bed", "red", "led"):
            return f"{w_prev} {w_end}"
    return None


@dataclass
class SensoryProfile:
    mean_concreteness: float = 3.0  # 1.0 (abstract) to 5.0 (concrete)
    concrete_ratio: float = 0.0     # % words with conc >= 3.5
    abstract_ratio: float = 0.0     # % words with conc <= 2.2
    dominant_modality: str = "none" # visual | auditory | haptic | gustatory | olfactory | interoceptive | action
    modality_scores: dict[str, float] = field(default_factory=dict)
    sensory_words: list[dict] = field(default_factory=list)
    abstract_words: list[str] = field(default_factory=list)


@dataclass
class EmotionProfile:
    valence: float = 0.5            # 0.0 (unpleasant) to 1.0 (pleasant)
    arousal: float = 0.5            # 0.0 (calm) to 1.0 (excited)
    dominance: float = 0.5          # 0.0 (submissive) to 1.0 (in control)
    dominant_emotion: str = "neutral"
    emotions: dict[str, int] = field(default_factory=dict)
    valence_label: str = "neutral"  # positive | negative | neutral
    arousal_label: str = "medium"   # high | medium | low


@dataclass
class LineStat:
    text: str
    syllables: int
    stress_grid: str
    word_count: int
    end_word: str | None
    rime_key: str | None
    unknown: list[str] = field(default_factory=list)
    hemistichs: tuple[int, int] | None = None
    ad_lib: str | None = None
    detail_type: str = "external"  # 'external' (E) or 'internal' (I)
@dataclass
class SectionStat:
    kind: str
    line_count: int
    contour: list[int]
    rhyme_scheme: str
    mean_syllables: float
    syllable_variance: float
    rhyme_density: float          # rhymed lines / total lines
    repeat_ratio: float           # repeated lines / total lines
    abstract_ratio: float
    sensory_ratio: float
    lines: list[LineStat] = field(default_factory=list)
    sensory_profile: SensoryProfile | None = None
    emotion_profile: EmotionProfile | None = None
    toggle_pattern: str = ""      # e.g. 'EEIE' (external/internal toggling)
    tripod_status: dict[str, bool] = field(default_factory=dict) # who, where, when
    @property
    def metric_consistency(self) -> float:
        """0..1. High means lines are near-uniform length (singable);
        low means the contour is ragged."""
        if not self.contour or self.mean_syllables == 0:
            return 1.0
        sd = self.syllable_variance ** 0.5
        return max(0.0, 1.0 - sd / self.mean_syllables)


class CraftAnalyzer:
    def __init__(self, ph: Phonetics | None = None,
                 db_conn: sqlite3.Connection | None = None,
                 db_path: Path | str | None = None) -> None:
        self.ph = ph or Phonetics()
        self._db: sqlite3.Connection | None = db_conn
        self._db_path = Path(db_path) if db_path else DEFAULT_DB
        self._get_word_sensory_cached = lru_cache(maxsize=32768)(self._word_sensory_uncached)
        self._get_word_emotion_cached = lru_cache(maxsize=32768)(self._word_emotion_uncached)

    @property
    def db(self) -> Any | None:
        if self._db is not None:
            return self._db
        if not (self._db_path and self._db_path.exists()):
            return None
        if str(self._db_path).endswith(".duckdb"):
            # Per-thread cursor on the process-wide read-only gateway.
            try:
                return Database.get(self._db_path, read_only=True).conn
            except Exception:
                return None
        try:
            self._db = sqlite3.connect(
                f"file:{self._db_path}?mode=ro", uri=True,
                check_same_thread=False)
        except sqlite3.OperationalError:
            return None
        return self._db

    def get_word_sensory(self, word: str) -> dict | None:
        if not word:
            return None
        res = self._get_word_sensory_cached(word.strip().lower())
        if not res:
            return None
        d = res.copy()
        if "modalities" in d and isinstance(d["modalities"], dict):
            d["modalities"] = d["modalities"].copy()
        return d

    def _word_sensory_uncached(self, w: str) -> dict | None:
        db = self.db
        if db is None:
            return None
        try:
            r1 = db.execute(
                "SELECT concreteness, dom_pos FROM sensory_concreteness WHERE word=?",
                (w,)).fetchone()
            r2 = db.execute(
                "SELECT auditory, gustatory, haptic, interoceptive, olfactory, visual, "
                "foot_leg, hand_arm, head, mouth, torso, dominant_modality, sensorimotor_score "
                "FROM sensory_sensorimotor WHERE word=?", (w,)).fetchone()
            if not r1 and not r2:
                return None
            return {
                "word": w,
                "concreteness": r1[0] if r1 else None,
                "dom_pos": r1[1] if r1 else None,
                "dominant_modality": r2[11] if r2 else None,
                "sensorimotor_score": r2[12] if r2 else None,
                "modalities": {
                    "auditory": r2[0], "gustatory": r2[1], "haptic": r2[2],
                    "interoceptive": r2[3], "olfactory": r2[4], "visual": r2[5],
                    "action": max(r2[6], r2[7], r2[8], r2[9], r2[10])
                } if r2 else {}
            }
        except Exception:
            return None

    def get_word_emotion(self, word: str) -> dict | None:
        if not word:
            return None
        res = self._get_word_emotion_cached(word.strip().lower())
        if not res:
            return None
        d = res.copy()
        if "emotions" in d and isinstance(d["emotions"], dict):
            d["emotions"] = d["emotions"].copy()
        return d

    def _word_emotion_uncached(self, w: str) -> dict | None:
        db = self.db
        if db is None:
            return None
        try:
            r1 = db.execute(
                "SELECT valence, arousal, dominance FROM emotion_vad WHERE word=?",
                (w,)).fetchone()
            r2 = db.execute(
                "SELECT anger, anticipation, disgust, fear, joy, sadness, surprise, trust, positive, negative, dominant_emotion "
                "FROM emotion_lexicon WHERE word=?", (w,)).fetchone()
            if not r1 and not r2:
                return None
            return {
                "word": w,
                "valence": r1[0] if r1 else None,
                "arousal": r1[1] if r1 else None,
                "dominance": r1[2] if r1 else None,
                "emotions": {
                    "anger": r2[0], "anticipation": r2[1], "disgust": r2[2],
                    "fear": r2[3], "joy": r2[4], "sadness": r2[5],
                    "surprise": r2[6], "trust": r2[7],
                    "positive": r2[8], "negative": r2[9]
                } if r2 else {},
                "dominant_emotion": r2[10] if r2 else "neutral"
            }
        except Exception:
            return None

    def analyze_sensory(self, text_or_lines: str | list[str]) -> SensoryProfile:
        if isinstance(text_or_lines, str):
            toks = _TOKEN.findall(text_or_lines.lower())
        else:
            toks = [t for l in text_or_lines for t in _TOKEN.findall(l.lower())]

        if not toks:
            return SensoryProfile()

        db = self.db
        if db is None:
            abstract_words = [t for t in toks if t in ABSTRACT_WORDS]
            sensory_words = [{"word": t, "concreteness": 4.5, "modality": "sensory"}
                             for t in toks if t in SENSORY_WORDS]
            abs_ratio = len(abstract_words) / len(toks)
            sens_ratio = len(sensory_words) / len(toks)
            return SensoryProfile(
                mean_concreteness=3.0,
                concrete_ratio=round(sens_ratio, 3),
                abstract_ratio=round(abs_ratio, 3),
                abstract_words=list(set(abstract_words)),
                sensory_words=sensory_words[:10],
            )

        concs: list[float] = []
        mod_sums = {
            "auditory": 0.0, "gustatory": 0.0, "haptic": 0.0,
            "interoceptive": 0.0, "olfactory": 0.0, "visual": 0.0, "action": 0.0
        }
        sensory_words = []
        abstract_words = set()

        for t in toks:
            info = self.get_word_sensory(t)
            if info:
                c = info.get("concreteness")
                if c is not None:
                    concs.append(c)
                    if c <= 2.2:
                        abstract_words.add(t)
                    elif c >= 3.5:
                        sensory_words.append({
                            "word": t,
                            "concreteness": round(c, 2),
                            "modality": info.get("dominant_modality") or "general"
                        })
                mods = info.get("modalities", {})
                for mk, mv in mods.items():
                    if mv and mv > 1.0:
                        mod_sums[mk] += mv
            elif t in ABSTRACT_WORDS:
                abstract_words.add(t)
            elif t in SENSORY_WORDS:
                sensory_words.append({"word": t, "concreteness": 4.5, "modality": "general"})

        n_toks = len(toks)
        mean_c = sum(concs) / len(concs) if concs else 3.0
        concrete_ratio = len(sensory_words) / n_toks
        abstract_ratio = len([t for t in toks if t in abstract_words or t in ABSTRACT_WORDS]) / n_toks

        total_mod_weight = sum(mod_sums.values())
        dom_mod = max(mod_sums, key=mod_sums.get) if total_mod_weight > 0 else "none"
        mod_dist = {k: round(v / total_mod_weight, 3) for k, v in mod_sums.items()} if total_mod_weight > 0 else {}

        return SensoryProfile(
            mean_concreteness=round(mean_c, 2),
            concrete_ratio=round(concrete_ratio, 3),
            abstract_ratio=round(abstract_ratio, 3),
            dominant_modality=dom_mod,
            modality_scores=mod_dist,
            sensory_words=sensory_words[:12],
            abstract_words=sorted(list(abstract_words))[:12],
        )

    def analyze_emotion(self, text_or_lines: str | list[str]) -> EmotionProfile:
        if isinstance(text_or_lines, str):
            toks = _TOKEN.findall(text_or_lines.lower())
        else:
            toks = [t for l in text_or_lines for t in _TOKEN.findall(l.lower())]

        if not toks or self.db is None:
            return EmotionProfile()

        vals, aros, doms = [], [], []
        emo_counts: dict[str, int] = {}

        for t in toks:
            e = self.get_word_emotion(t)
            if e:
                v, a, d = e.get("valence"), e.get("arousal"), e.get("dominance")
                if v is not None:
                    vals.append(v)
                if a is not None:
                    aros.append(a)
                if d is not None:
                    doms.append(d)
                for emo_name, emo_val in e.get("emotions", {}).items():
                    if emo_val == 1 and emo_name not in ("positive", "negative"):
                        emo_counts[emo_name] = emo_counts.get(emo_name, 0) + 1

        mean_v = sum(vals) / len(vals) if vals else 0.5
        mean_a = sum(aros) / len(aros) if aros else 0.5
        mean_d = sum(doms) / len(doms) if doms else 0.5

        v_label = "positive" if mean_v >= 0.58 else ("negative" if mean_v <= 0.42 else "neutral")
        a_label = "high" if mean_a >= 0.55 else ("low" if mean_a <= 0.45 else "medium")
        dom_emo = max(emo_counts, key=emo_counts.get) if emo_counts else "neutral"

        return EmotionProfile(
            valence=round(mean_v, 3),
            arousal=round(mean_a, 3),
            dominance=round(mean_d, 3),
            dominant_emotion=dom_emo,
            emotions=emo_counts,
            valence_label=v_label,
            arousal_label=a_label,
        )
    # -- line level ---------------------------------------------------
    def line_stat(self, text: str) -> LineStat:
        toks = _TOKEN.findall(text.lower())
        syl = 0
        grid: list[str] = []
        unknown: list[str] = []
        for t in toks:
            p = self.ph.best(t)
            if p is not None:
                syl += p.syllables
                grid.append(p.stress_pattern)
            else:
                unknown.append(t)
                s_est = self.ph.syllables(t) or 1
                syl += s_est
                grid.append("1" + "0" * max(0, s_est - 1))
        # Isolate trailing musical ad-lib / backing echo: e.g. "(Yeah)" or "(Wreck)"
        # Isolate parenthetical ad-lib, e.g. "(yeah)". If text has trailing words after "(adlib)",
        # the true lyric ending is the word immediately before the parenthetical tag.
        m_adlib = re.search(r"\s*\(([^)]+)\)\s*(.*?)$", text)
        ad_lib = m_adlib.group(1).strip() if m_adlib else None
        # Strip any parenthetical tag and anything following it to find the real sung anchor
        lead_text = re.sub(r"\s*\([^)]*\).*$", "", text).strip() if m_adlib else text
        lead_toks = _TOKEN.findall(lead_text.lower())
        # "I love my baby, ah": the rhyme word is "baby", not the tag "ah".
        while len(lead_toks) > 1 and lead_toks[-1] in VOCABLE_TAGS:
            lead_toks.pop()
        end = lead_toks[-1] if lead_toks else (toks[-1] if toks else None)
        ep = self.ph.best(end) if end else None
        hemistichs = None
        if "//" in lead_text:
            parts = lead_text.split("//", 1)
            hemistichs = (self.line_stat(parts[0]).syllables, self.line_stat(parts[1]).syllables)
        elif "," in lead_text:
            parts = lead_text.split(",", 1)
            hemistichs = (self.line_stat(parts[0]).syllables, self.line_stat(parts[1]).syllables)

        return LineStat(
            text=text,
            syllables=syl,
            stress_grid="".join(grid),
            word_count=len(toks),
            end_word=end,
            rime_key=ep.rime_stressed() if ep else None,
            unknown=unknown,
            hemistichs=hemistichs,
            ad_lib=ad_lib,
            detail_type=self.classify_detail_type(text, toks),
        )

    def classify_detail_type(self, text: str, toks: list[str] | None = None) -> str:
        """Classify a line as External (E) or Internal (I).

        External: Concrete sensory nouns, tactile objects, specific physical actions
        Internal: Cognitive/evaluative verbs, abstract nouns, emotion markers, internal thoughts
        """
        if toks is None:
            toks = _TOKEN.findall(text.lower())
        if not toks:
            return "external"

        # Internal indicators (thoughts, feelings, evaluations)
        internal_markers = {
            "think", "thought", "thinking", "feel", "felt", "feeling", "feelings",
            "wonder", "wondered", "wondering", "know", "knew", "knowing",
            "wish", "wished", "wishing", "hope", "hoped", "hoping",
            "miss", "missed", "missing", "love", "hate", "scared", "afraid",
            "lonely", "alone", "sad", "sadness", "happy", "happiness",
            "trying", "tried", "needed", "needs", "wanted", "wants",
            "believe", "believed", "believing", "remember", "remembered",
            "dream", "dreams", "dreaming", "pray", "prayed", "praying",
            "gloom", "pain", "hurt", "grief", "truth", "lies", "guilt",
            "soul", "heart", "destiny", "fate", "forever", "eternity",
            "confused", "confusion", "disgusted", "bearable", "unbearable",
        }
        # Physical external anchors
        external_markers = SENSORY_WORDS | {
            "chair", "table", "gate", "bag", "pocket", "button", "suit",
            "hallway", "bus", "train", "flight", "terminal", "screen",
            "clock", "shoes", "cup", "coffee", "counter", "flicker", "wire",
            "cable", "keys", "lamp", "curtain", "glass", "floor", "ceiling",
            "slumped", "spit", "humming", "pacing", "shook", "stumbled",
            "holding", "held", "poured", "spilled", "shuffled", "dropped",
        }

        i_count = sum(1 for t in toks if t in internal_markers or t in ABSTRACT_WORDS)
        e_count = sum(1 for t in toks if t in external_markers)

        sens_prof = self.analyze_sensory(text)
        if sens_prof.concrete_ratio > 0.35 and e_count >= i_count:
            return "external"
        if sens_prof.abstract_ratio > 0.20 or i_count > e_count:
            return "internal"
        if e_count > 0:
            return "external"
        return "internal" if i_count > 0 else "external"
    # -- section level ------------------------------------------------
    def section_stat(self, lines: list[str], kind: str = "verse") -> SectionStat:
        stats = [self.line_stat(l) for l in lines if _TOKEN.findall(l.lower())]
        if not stats:
            return SectionStat(kind, 0, [], "", 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

        contour = [s.syllables for s in stats]
        mean = sum(contour) / len(contour)
        var = sum((c - mean) ** 2 for c in contour) / len(contour)

        keys = [s.rime_key for s in stats]
        scheme = self._scheme(keys)
        rhymed = sum(1 for i, k in enumerate(keys)
                     if k and any(k == keys[j] for j in range(len(keys)) if j != i))
        density = rhymed / len(stats)

        norms = [" ".join(_TOKEN.findall(s.text.lower())) for s in stats]
        repeats = len(norms) - len(set(norms))

        sens_prof = self.analyze_sensory(lines)
        emo_prof = self.analyze_emotion(lines)

        # Toggle pattern (E = External, I = Internal)
        toggle_pattern = "".join("E" if s.detail_type == "external" else "I" for s in stats)

        # Tripod check (Who, Where, When) over opening lines
        tripod_status = self.check_tripod(lines[:3])

        return SectionStat(
            kind=kind,
            line_count=len(stats),
            contour=contour,
            rhyme_scheme=scheme,
            mean_syllables=round(mean, 2),
            syllable_variance=round(var, 2),
            rhyme_density=round(density, 3),
            repeat_ratio=round(repeats / len(stats), 3),
            abstract_ratio=sens_prof.abstract_ratio,
            sensory_ratio=sens_prof.concrete_ratio,
            lines=stats,
            sensory_profile=sens_prof,
            emotion_profile=emo_prof,
            toggle_pattern=toggle_pattern,
            tripod_status=tripod_status,
        )

    def check_tripod(self, opening_lines: list[str]) -> dict[str, bool]:
        """Verify the three elements of connection: Who, Where, When."""
        all_text = " ".join(opening_lines).lower()
        toks = set(_TOKEN.findall(all_text))

        # Who: pronouns, proper nouns, characters
        who_markers = {"i", "me", "my", "we", "us", "our", "you", "your", "he", "him", "his", "she", "her", "they", "them"}
        has_who = bool(toks & who_markers)

        # Where: concrete physical environment / setting anchors
        where_markers = SENSORY_WORDS | {
            "room", "gate", "station", "terminal", "airport", "hall", "street", "highway",
            "bed", "kitchen", "porch", "counter", "bar", "booth", "car", "bus", "train",
            "city", "town", "basement", "corridor", "desk", "sidewalk", "window"
        }
        has_where = bool(toks & where_markers)

        # When: temporal words or clear verb tense anchoring
        when_markers = {
            "now", "night", "morning", "midnight", "afternoon", "dusk", "dawn", "today",
            "yesterday", "tomorrow", "summer", "winter", "fall", "spring", "clock", "three",
            "two", "am", "pm", "hours", "minutes", "waiting", "standing", "sitting", "lying"
        }
        has_when = bool(toks & when_markers)

        return {"who": has_who, "where": has_where, "when": has_when}

    @staticmethod
    def _scheme(keys: list[str | None], max_letters: int = 8) -> str:
        labels: dict[str, str] = {}
        out: list[str] = []
        nxt = 0
        for k in keys:
            if not k:
                out.append("-")
                continue
            if k not in labels:
                if nxt >= max_letters:
                    out.append("-")
                    continue
                labels[k] = chr(ord("A") + nxt)
                nxt += 1
            out.append(labels[k])
        return "".join(out)

    # -- internal / cross-line rhyme ----------------------------------
    def internal_rhymes(self, text: str, min_strength: float = 0.55
                        ) -> list[tuple[str, str, str]]:
        """Rhymes *within* one line. Returns (word_a, word_b, kind).

        Modern lyric density comes largely from internal rhyme, which
        end-rhyme-only analysis misses entirely.
        """
        toks = _TOKEN.findall(text.lower())
        out: list[tuple[str, str, str]] = []
        for i in range(len(toks)):
            for j in range(i + 1, len(toks)):
                if toks[i] == toks[j]:
                    continue
                j_ = self.ph.compare(toks[i], toks[j])
                if j_.strength >= min_strength and j_.kind not in {"none", "identity"}:
                    out.append((toks[i], toks[j], j_.kind))
        return out

    def cross_line_rhymes(self, lines: list[str], min_strength: float = 0.55
                          ) -> list[tuple[int, int, str, str, str]]:
        """Rhymes between non-final words of different lines.

        Returns (line_i, line_j, word_a, word_b, kind).
        """
        tok_lines = [_TOKEN.findall(l.lower()) for l in lines]
        out: list[tuple[int, int, str, str, str]] = []
        for a in range(len(tok_lines)):
            for b in range(a + 1, len(tok_lines)):
                for wa in tok_lines[a][:-1]:
                    for wb in tok_lines[b][:-1]:
                        if wa == wb:
                            continue
                        j = self.ph.compare(wa, wb)
                        if j.strength >= min_strength and j.kind not in {
                                "none", "identity"}:
                            out.append((a, b, wa, wb, j.kind))
        return out

    # -- prosody ------------------------------------------------------
    def prosody_collisions(self, text: str, beats: int = 4
                           ) -> list[tuple[int, str, str]]:
        """Flag unstressed syllables landing on strong beats.

        Returns (syllable_index, word, reason). Assumes an even syllable
        distribution across `beats`, which is a rough model -- real placement
        needs a melody. Still catches the common case where a multi-syllable
        word's weak syllable sits where the pulse falls.
        """
        toks = _TOKEN.findall(text.lower())
        slots: list[tuple[str, int]] = []
        for t in toks:
            p = self.ph.best(t)
            if p is None:
                continue
            pat = p.stress_pattern
            if len(pat) == 1:
                # monosyllables are contextually stressable: never a collision
                slots.append((t, 1))
            else:
                for ch in pat:
                    slots.append((t, int(ch)))
        if not slots:
            return []

        n = len(slots)
        step = n / beats if beats else n
        out: list[tuple[int, str, str]] = []
        for b in range(beats):
            idx = int(round(b * step))
            if idx >= n:
                break
            word, stress = slots[idx]
            if stress == 0:
                out.append((idx, word, f"unstressed syllable on beat {b + 1}"))
        return out

    # -- readability --------------------------------------------------
    def flesch_kincaid_grade(self, lines: list[str]) -> float | None:
        """FK grade level, treating each line as a sentence."""
        stats = [self.line_stat(l) for l in lines if _TOKEN.findall(l.lower())]
        if not stats:
            return None
        words = sum(s.word_count for s in stats)
        syls = sum(s.syllables for s in stats)
        sents = len(stats)
        if words == 0:
            return None
        return round(0.39 * (words / sents) + 11.8 * (syls / words) - 15.59, 2)
