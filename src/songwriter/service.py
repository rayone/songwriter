"""Service layer: compact, decision-ready answers for a model client.

Sizing rules, because the primary consumer is a small model:
  * every result is capped and pre-ranked; the caller never filters
  * verdicts are labels and integers, not prose
  * corpus frequency is folded in so "is this a cliche" is measured
  * nothing returns more than a few hundred tokens of payload

Corpus lookups degrade gracefully: if the DB is absent or still building,
the phonetic answers still work and corpus fields come back as None.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any
import duckdb

from songwriter.db import Database

from songwriter.craft import CraftAnalyzer, FormValidator
from songwriter.phonetics import Phonetics
from songwriter.corpus.search import SongDetail, SongSearcher, SongSummary

from songwriter.config import CORPUS_DB as DEFAULT_DB

def _pair_bucket(pair_freq: int | None) -> int:
    """Coarse freshness bucket for a rhyme pairing.

    Sorting on raw pair_freq puts every unused pairing ahead of everything
    else, which lets a weak assonance match outrank a strong perfect rhyme.
    Bucketing on a log-ish scale keeps freshness the primary signal while
    letting phonetic strength decide within a bucket.
    """
    if pair_freq is None:
        return 1          # unknown: treat as mid, not best
    if pair_freq == 0:
        return 0
    if pair_freq < 50:
        return 1
    if pair_freq < 500:
        return 2
    if pair_freq < 5000:
        return 3
    return 4

_MOOD_TARGETS: dict[str, tuple[float, float]] = {
    "melancholy": (0.20, 0.25),
    "sad": (0.20, 0.25),
    "dark": (0.18, 0.40),
    "depressing": (0.18, 0.40),
    "angry": (0.20, 0.80),
    "aggressive": (0.20, 0.85),
    "happy": (0.85, 0.65),
    "joyful": (0.88, 0.70),
    "uplifting": (0.85, 0.65),
    "calm": (0.70, 0.20),
    "peaceful": (0.72, 0.18),
    "romantic": (0.80, 0.45),
    "love": (0.82, 0.50),
    "energetic": (0.75, 0.85),
    "excited": (0.80, 0.85),
    "anxious": (0.25, 0.75),
    "fear": (0.20, 0.75),
}

# ARPABET nucleus -> plain-English rhyme-sound label, so a writer can pick a
# "target rhyme sound" without knowing phonetics.
_VOWEL_LABEL: dict[str, str] = {
    "OW": "Long O", "AO": "Short O / aw", "AA": "Short O / ah", "AE": "Short A",
    "EY": "Long A", "AY": "Long I", "IY": "Long E", "IH": "Short I",
    "EH": "Short E", "AH": "Schwa / uh", "UW": "Long U / oo", "UH": "Short U",
    "ER": "R-colored (er)", "AW": "Ow (now)", "OY": "Oy (boy)",
}
# Pure function words: articles, prepositions, conjunctions, pronouns and
# auxiliary/modal verbs. A corpus-overused window counts as a cliche only if it
# carries at least two words OUTSIDE this set, so grammatical glue like "what
# you" or "in the" is never flagged while figurative units ("let it go",
# "morning light", "never let") still are. Light/lexical verbs (let, go, give,
# take...) are deliberately NOT here: they are the backbone of real idioms.
# Distinct from TRAFFIC_DIRECTOR_WORDS, which is tuned for stressed-beat
# scansion and keeps pronouns as candidate beats.
_CLICHE_STOP = frozenset({
    "a", "an", "the", "and", "or", "but", "nor", "so", "if", "as", "of", "to",
    "in", "on", "at", "by", "for", "with", "from", "into", "onto",
    "than", "then", "that", "this", "these", "those",
    "i", "you", "he", "she", "it", "we", "they", "me", "him", "her", "us", "them",
    "my", "your", "his", "its", "our", "their", "mine", "yours",
    "am", "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "have", "has", "had", "will", "would", "shall", "should",
    "can", "could", "may", "might", "must", "not", "no",
    "what", "who", "when", "where", "why", "how",
    # Phrasal-verb particles: "comes down", "turn around", "hold on" are plain
    # English, not tropes. With these as glue, a window needs two real content
    # words ("morning light", "let it go") to count as a cliche.
    "down", "up", "out", "off", "away", "back", "over", "around", "through",
    "about", "along", "again", "here", "there", "now", "just", "all",
    "every", "each", "one", "some", "any", "other", "another",
})

@dataclass
class RhymeCandidate:
    word: str
    kind: str
    syllables: int
    shared_syllables: int
    strength: float
    corpus_freq: int | None = None      # how common the word is in lyrics
    pair_freq: int | None = None        # how often this pair is used together
    verdict: str = ""                   # fresh | common | cliche | unknown
    sources: list[str] = field(default_factory=list)
    rhyme_ending: str = ""              # masculine | feminine | triple
    rhyme_class: str = ""               # perfect | family | additive_subtractive | assonance | consonance
    concreteness: float | None = None   # 1.0 (abstract) to 5.0 (concrete)
    dominant_modality: str | None = None# visual | auditory | haptic | gustatory | olfactory | interoceptive | action
    valence: float | None = None        # 0.0 (unpleasant) to 1.0 (pleasant)
    arousal: float | None = None        # 0.0 (calm) to 1.0 (excited)
    score: float | None = None          # multi-objective ranking score

@dataclass
class LineReport:
    text: str
    syllables: int
    stress_grid: str
    end_word: str | None
    unknown_words: list[str] = field(default_factory=list)
    prosody_flags: list[str] = field(default_factory=list)
    internal_rhymes: list[str] = field(default_factory=list)


@dataclass
class DraftReport:
    line_count: int
    total_syllables: int
    rhyme_scheme: str
    rhyme_density: float
    metric_consistency: float
    mean_syllables: float
    contour: list[int] = field(default_factory=list)
    abstract_ratio: float = 0.0
    sensory_ratio: float = 0.0
    repeat_ratio: float = 0.0
    readability_grade: float | None = None
    issues: list[str] = field(default_factory=list)
    dominant_modality: str = "none"
    mean_concreteness: float = 3.0
    valence: float = 0.5
    arousal: float = 0.5
    dominant_emotion: str = "neutral"
    sensory_anchors: list[str] = field(default_factory=list)
    toggle_pattern: str = ""
    tripod_status: dict[str, bool] = field(default_factory=dict)

@dataclass
class ExemplarLine:
    text: str
    syllables: int
    stress_grid: str = ""
    end_word: str = ""
    rhyme_tag: str = ""


@dataclass
class SectionExemplar:
    song_title: str
    artist: str
    section_name: str
    kind: str
    lines: list[ExemplarLine]
    rhyme_scheme: str = ""
    contour: list[int] = field(default_factory=list)

    def format_stencil(self) -> str:
        out = [
            f"EXEMPLAR RHYTHMIC STENCIL (from '{self.song_title}' by {self.artist}, {self.section_name}):",
            "Observe the line lengths, phrasing rhythm, and rhyme bindings:"
        ]
        for i, l in enumerate(self.lines):
            tag = f" [Rhyme {l.rhyme_tag}]" if l.rhyme_tag else ""
            out.append(f"  {i+1}. {l.text:<50} ({l.syllables} syl){tag}")
        return "\n".join(out)

class Workbench:
    """Single entry point for all tools."""

    def __init__(self, db_path: Path | str | None = None,
                 accent: str = "en-US") -> None:
        self._db: Any | None = None
        self._db_path = Path(db_path) if db_path else DEFAULT_DB
        self.ph = Phonetics(accent=accent)
        self.ca = CraftAnalyzer(self.ph, db_path=self._db_path)
        self.fv = FormValidator(self.ca)
        self.searcher = SongSearcher(sqlite_path=self._db_path)
        self._word_freq = lru_cache(maxsize=32768)(self._word_freq_uncached)
        self._pair_freq = lru_cache(maxsize=32768)(self._pair_freq_uncached)
        self.phrase_freq = lru_cache(maxsize=32768)(self._phrase_freq_uncached)
    @property
    def db(self) -> Any | None:
        if not self._db_path.exists():
            return None
        if str(self._db_path).endswith(".duckdb"):
            # Per-thread cursor on the process-wide read-only gateway.
            try:
                return Database.get(self._db_path, read_only=True).conn
            except Exception:
                return None
        if self._db is None:
            try:
                self._db = sqlite3.connect(
                    f"file:{self._db_path}?mode=ro", uri=True,
                    check_same_thread=False)
            except sqlite3.OperationalError:
                return None
        return self._db

    def _word_freq_uncached(self, word: str) -> int | None:
        db = self.db
        if db is None:
            return None
        try:
            r = db.execute(
                "SELECT corpus_freq FROM lexicon WHERE word=? AND word_count=1", (word.lower(),)
            ).fetchone()
            if r is not None and r[0] is not None:
                return r[0]
        except Exception:
            pass
        try:
            r = db.execute(
                "SELECT v.freq FROM corpus_vocab v JOIN word w ON w.id=v.word_id "
                "WHERE w.normalized=? AND w.word_count=1", (word,)).fetchone()
            return r[0] if r else 0
        except Exception:
            return None

    def _pair_freq_uncached(self, a: str, b: str) -> int | None:
        db = self.db
        if db is None:
            return None
        lo, hi = sorted((a.lower(), b.lower()))
        try:
            r = db.execute(
                "SELECT pair_freq FROM corpus_rhyme_stat "
                "WHERE word_a=? AND word_b=?", (lo, hi)).fetchone()
            if r is not None and r[0] is not None:
                return r[0]
        except Exception:
            pass
        try:
            r = db.execute(
                "SELECT s.pair_freq FROM corpus_rhyme_stat s "
                "JOIN word wa ON wa.id=s.word_a JOIN word wb ON wb.id=s.word_b "
                "WHERE wa.normalized=? AND wb.normalized=?", (lo, hi)).fetchone()
            return r[0] if r else 0
        except Exception:
            return None
    def _phrase_freq_uncached(self, phrase: str) -> int | None:
        """Corpus frequency of an exact phrase (2-5 words)."""
        db = self.db
        if db is None:
            return None
        toks = phrase.lower().split()
        if not 2 <= len(toks) <= 5:
            return None
        try:
            r = db.execute(
                "SELECT freq FROM corpus_ngram WHERE gram=? AND n=?",
                (" ".join(toks), len(toks))).fetchone()
            return r[0] if r else 0
        except Exception:
            return None

    def line_cliches(self, line: str, min_freq: int = 2000, top: int = 3,
                     allowlist: frozenset[str] | set[str] | None = None) -> list[tuple[str, int]]:
        """Overused *content* phrases in a line, as [(phrase, freq), ...] strongest first.

        Scans every 2-5 word window against corpus_ngram. A window counts only if
        it carries at least two words outside _CLICHE_STOP (pure function words,
        pronouns, auxiliaries), so grammatical glue ("in the", "what you") is
        never flagged while figurative units ("let it go", "morning light") are.
        Any window matching ``allowlist`` (familiar conversational idioms such as
        "no one knows") is skipped so natural speech is not policed. Overlapping
        windows that share a word span collapse to the single highest-frequency
        phrase, so the result is distinct phrases, not nested fragments.
        """
        db = self.db
        if db is None:
            return []
        allow = {p.strip().lower() for p in (allowlist or ()) if p.strip()}
        toks = [t for t in re.findall(r"[a-z']+", line.lower()) if t]
        if len(toks) < 2:
            return []
        hits: list[tuple[int, int, str, int]] = []  # (start, end, phrase, freq)
        for n in (5, 4, 3, 2):
            for i in range(0, len(toks) - n + 1):
                window = toks[i:i + n]
                gram = " ".join(window)
                if gram in allow:
                    continue
                lexical = [w for w in window if w not in _CLICHE_STOP]
                if len(lexical) < 2:
                    continue
                f = self.phrase_freq(gram)
                if f and f >= min_freq:
                    hits.append((i, i + n, gram, f))
        hits.sort(key=lambda h: h[3], reverse=True)
        chosen: list[tuple[str, int]] = []
        used: set[int] = set()
        for start, end, phrase, freq in hits:
            span = set(range(start, end))
            if span & used:
                continue  # already covered by a stronger, overlapping phrase
            used |= span
            chosen.append((phrase, freq))
            if len(chosen) >= top:
                break
        return chosen
    @staticmethod
    def _verdict(pair_freq: int | None) -> str:
        if pair_freq is None:
            return "unknown"
        if pair_freq == 0:
            return "fresh"
        if pair_freq < 50:
            return "uncommon"
        if pair_freq < 500:
            return "common"
        return "cliche"

    # -- sound-keyed rhyme lookup -------------------------------------
    def _rhymes_sql(self, word: str, min_syl: int, max_syl: int | None,
                    min_word_freq: int, limit: int
                    ) -> list[tuple[str, str, int, float]] | None:
        """Perfect rhymes via the rime_family index.

        Replaces a full CMUdict scan (1.7s) with an indexed lookup on
        (rime_key, accent). Returns (word, kind, depth, strength) tuples, or
        None if the index has not been built so the caller can fall back.
        """
        db = self.db
        if db is None:
            return None
        src = self.ph.resolve(word)
        if src is None:
            return []
        rime = src.rime_stressed()
        if not rime:
            return []
        try:
            lex_rows = db.execute(
                "SELECT word, syllable_count, perfect_depth FROM lexicon "
                "WHERE perfect_tail IS NOT NULL AND perfect_tail = ("
                "  SELECT perfect_tail FROM lexicon WHERE word = ? LIMIT 1"
                ") AND word != ? AND (corpus_freq IS NULL OR corpus_freq >= ?) "
                "ORDER BY corpus_freq DESC LIMIT ?",
                (word.lower(), word.lower(), min_word_freq, limit)
            ).fetchall()
            if lex_rows:
                out = []
                for cand, syl, depth in lex_rows:
                    d = depth or 1
                    out.append((cand, "perfect", d, min(1.0, 0.8 + 0.07 * (d - 1))))
                return out
        except Exception:
            pass
        try:
            rows = db.execute(
                "SELECT w.normalized, m.syllables FROM rime_family f "
                "JOIN rime_member m ON m.family_id = f.id "
                "JOIN word w ON w.id = m.word_id "
                "WHERE f.rime_key = ? AND f.accent = ? "
                "  AND w.normalized != ? "
                "  AND (m.corpus_freq IS NULL OR m.corpus_freq >= ?) "
                "ORDER BY m.corpus_freq DESC LIMIT ?",
                (rime, self.ph.accent, word.lower(), min_word_freq, limit)
            ).fetchall()
        except (sqlite3.OperationalError, duckdb.Error, Exception):
            return None

        out: list[tuple[str, str, int, float]] = []
        for cand, syl in rows:
            p = self.ph.best(cand) or self.ph.phrase_prons(cand)
            if p is None:
                continue
            depth = 0
            for d in range(1, min(src.syllables, p.syllables) + 1):
                if src.rime(d) and src.rime(d) == p.rime(d):
                    depth = d
                else:
                    break
            depth = max(depth, 1)
            out.append((cand, "perfect", depth, min(1.0, 0.8 + 0.07 * (depth - 1))))
        return out

    def _near_rhymes_sql(self, word: str, min_word_freq: int, limit: int
                         ) -> list[tuple[str, str, int, float]] | None:
        """Slant / assonance / consonance candidates via the rime index.

        Exact-rime lookup cannot answer these, so without this the indexed
        fast path would silently return zero slant results. Candidates are
        drawn from families sharing the stressed nucleus (assonance) or the
        coda (consonance), then classified by the phonetic engine so the
        labels stay consistent with compare().
        """
        db = self.db
        if db is None:
            return None
        src = self.ph.resolve(word)
        if src is None:
            return []
        nucleus, coda = src.nucleus, src.coda
        if not nucleus:
            return []
        try:
            lex_rows = db.execute(
                "SELECT word, syllable_count FROM lexicon "
                "WHERE ((assonance_tail IS NOT NULL AND assonance_tail = ("
                "  SELECT assonance_tail FROM lexicon WHERE word = ? LIMIT 1"
                ")) OR (consonance_tail IS NOT NULL AND consonance_tail = ("
                "  SELECT consonance_tail FROM lexicon WHERE word = ? LIMIT 1"
                "))) AND word != ? AND (corpus_freq IS NULL OR corpus_freq >= ?) "
                "ORDER BY corpus_freq DESC LIMIT ?",
                (word.lower(), word.lower(), word.lower(), max(min_word_freq, 1), limit)
            ).fetchall()
            if lex_rows:
                out = []
                for (cand, syl) in lex_rows:
                    j = self.ph.compare(word, cand)
                    if j.kind in {"none", "identity", "perfect"}:
                        continue
                    out.append((cand, j.kind, j.shared_syllables, j.strength))
                return out
        except Exception:
            pass
        try:
            rows = db.execute(
                "SELECT w.normalized FROM rime_family f "
                "JOIN rime_member m ON m.family_id = f.id "
                "JOIN word w ON w.id = m.word_id "
                "WHERE f.accent = ? AND w.normalized != ? "
                "  AND m.corpus_freq >= ? "
                "  AND (f.rime_key LIKE ? OR f.rime_key LIKE ?) "
                "ORDER BY m.corpus_freq DESC LIMIT ?",
                (self.ph.accent, word.lower(), max(min_word_freq, 1),
                 f"{nucleus}%", f"%{coda}" if coda else f"{nucleus}%",
                 limit)).fetchall()
        except (sqlite3.OperationalError, duckdb.Error, Exception):
            return None

        out: list[tuple[str, str, int, float]] = []
        for (cand,) in rows:
            j = self.ph.compare(word, cand)
            if j.kind in {"none", "identity", "perfect"}:
                continue
            out.append((cand, j.kind, j.shared_syllables, j.strength))
        return out

    def bridged_rhymes(self, word: str, limit: int = 12,
                       min_weight: int = 2) -> list[dict]:
        """Near rhymes a human editor asserted, from the editorial rhyme source.

        A bridge links two rime families that the source's surprising-rhyme
        blocks pair up. Of strongly-bridged pairs, the computed verdict is
        mostly assonance/none/weak and almost never 'perfect' -- so these are
        exactly the slant rhymes a phonetic engine rejects but a songwriter
        uses. There is no computational substitute for this signal.
        """
        db = self.db
        if db is None:
            return []
        src = self.ph.resolve(word)
        if src is None:
            return []
        rime = src.rime_stressed()
        if not rime:
            return []
        try:
            rows = db.execute(
                "SELECT w.normalized, f2.rime_key, b.weight, b.exemplar, "
                "       m.corpus_freq, m.syllables "
                "FROM rime_family f1 "
                "JOIN rime_bridge b ON b.from_family = f1.id "
                "JOIN rime_family f2 ON f2.id = b.to_family "
                "JOIN rime_member m ON m.family_id = f2.id "
                "JOIN word w ON w.id = m.word_id "
                "WHERE f1.rime_key = ? AND f1.accent = 'en-GB-nonrhotic' "
                "  AND b.weight >= ? AND w.normalized != ? "
                "  AND m.corpus_freq >= 500 "
                "ORDER BY b.weight DESC, m.corpus_freq DESC LIMIT ?",
                (rime, min_weight, word.lower(), limit * 3)).fetchall()
        except (sqlite3.OperationalError, duckdb.Error, Exception):
            return []

        out: list[dict] = []
        seen: set[str] = set()
        for cand, key, weight, exemplar, freq, syl in rows:
            if cand in seen:
                continue
            seen.add(cand)
            pf = self._pair_freq(word.lower(), cand)
            out.append({"word": cand, "rime": key, "syllables": syl,
                        "editorial_weight": weight, "section_exemplar": exemplar,
                        "computed": self.ph.compare(word, cand).kind,
                        "corpus_freq": freq, "pair_freq": pf,
                        "verdict": self._verdict(pf)})
            if len(out) >= limit:
                break
        return out

    # -- dictionary-backed lookups ------------------------------------
    def phrase_rhymes(self, word: str, limit: int = 10) -> list[dict]:
        """Multi-word phrases that rhyme with `word`.

        Sourced from the slang source's explicit "Rhymes on X" assertions. CMUdict
        cannot represent a multi-word rhyme at all, so this is editorial data
        with no computational substitute -- and phrase rhymes are what produce
        conversational pairings instead of single-word end rhymes.
        """
        db = self.db
        if db is None:
            return []
        try:
            rows = db.execute(
                "SELECT wp.normalized, wp.word_count, "
                "       GROUP_CONCAT(DISTINCT t.tag) "
                "FROM phrase_substitution ps "
                "JOIN word wt ON wt.id = ps.target_word_id "
                "JOIN word wp ON wp.id = ps.phrase_word_id "
                "LEFT JOIN word_tag t ON t.word_id = wp.id "
                "     AND t.tag_type = 'region' "
                "WHERE wt.normalized = ? AND wp.word_count > 1 "
                "GROUP BY wp.id ORDER BY wp.word_count, wp.normalized LIMIT ?",
                (word.lower(), limit)).fetchall()
        except (sqlite3.OperationalError, duckdb.Error, Exception):
            return []
        return [{"phrase": p, "words": n,
                 "regions": (tags.split(",") if tags else [])}
                for p, n, tags in rows]

    def surprising_rhymes(self, word: str, limit: int = 12) -> list[dict]:
        """Rhymes drawn from the editorial surprising-rhyme blocks.

        These are editorial judgements about which unexpected rhymes actually
        land. Phonetics can enumerate every neighbour mechanically but cannot
        rank them by interest, so this is the taste layer.

        Results are British non-rhotic by source, and labelled as such.
        """
        db = self.db
        if db is None:
            return []
        try:
            rows = db.execute(
                "SELECT DISTINCT w2.normalized, m2.tier, g.external_ref "
                "FROM word w1 "
                "JOIN group_member m1 ON m1.word_id = w1.id "
                "JOIN rhyme_group g ON g.id = m1.group_id "
                "JOIN group_member m2 ON m2.group_id = g.id "
                "JOIN word w2 ON w2.id = m2.word_id "
                "WHERE w1.normalized = ? AND g.is_surprising = 1 "
                "  AND w2.id != w1.id "
                "LIMIT ?", (word.lower(), limit * 4)).fetchall()
        except (sqlite3.OperationalError, duckdb.Error, Exception):
            return []

        out: list[dict] = []
        for cand, tier, ref in rows:
            wf = self._word_freq(cand)
            if wf is not None and wf < 50:
                continue
            pf = self._pair_freq(word.lower(), cand)
            out.append({"word": cand, "syllable_tier": tier, "section": ref,
                        "corpus_freq": wf, "pair_freq": pf,
                        "verdict": self._verdict(pf),
                        "accent": "en-GB-nonrhotic"})
        out.sort(key=lambda d: (_pair_bucket(d["pair_freq"]),
                                -(d["corpus_freq"] or 0)))
        return out[:limit]

    def word_tags(self, word: str) -> dict:
        """Register and region tags for a word or phrase.

        Slang, vulgar and offensive registers are stored and returned as-is.
        Filtering is a caller decision, not an ingest-time one.
        """
        db = self.db
        if db is None:
            return {}
        try:
            rows = db.execute(
                "SELECT t.tag_type, t.tag FROM word_tag t "
                "JOIN word w ON w.id = t.word_id WHERE w.normalized = ?",
                (word.lower(),)).fetchall()
        except (sqlite3.OperationalError, duckdb.Error, Exception):
            return {}
        out: dict[str, list[str]] = {}
        for tag_type, tag in rows:
            out.setdefault(tag_type, []).append(tag)
        return out

    def is_unrhymable(self, word: str) -> bool | None:
        """Whether a source asserts this word has no rhyme.

        A negative assertion, which CMUdict cannot produce.
        """
        db = self.db
        if db is None:
            return None
        try:
            r = db.execute(
                "SELECT is_unrhymable FROM lexicon WHERE word = ? LIMIT 1",
                (word.lower(),)).fetchone()
            if r is not None:
                return bool(r[0])
        except Exception:
            pass
        try:
            r = db.execute(
                "SELECT 1 FROM unrhymable u JOIN word w ON w.id = u.word_id "
                "WHERE w.normalized = ? LIMIT 1", (word.lower(),)).fetchone()
        except Exception:
            return None
        return r is not None

    def starter_prompts(self, limit: int = 8) -> list[dict]:
        """Craft cues from the songwriting guide, as pointers into the file."""
        db = self.db
        if db is None:
            return []
        try:
            rows = db.execute(
                "SELECT part, cue, stage, source_ref FROM starter_prompt "
                "ORDER BY id LIMIT ?", (limit,)).fetchall()
        except (sqlite3.OperationalError, duckdb.Error, Exception):
            return []
        return [{"part": p, "cue": c, "stage": s, "ref": r}
                for p, c, s, r in rows]


    def dictionary_rhymes(self, word: str, limit: int = 50) -> list[dict]:
        """Rhymes from the unified independent rhyme database."""
        db = self.db
        if db is None:
            return []
        try:
            rows = db.execute("""
                SELECT rhyme_word, syllables, kind, source_count
                FROM unified_rhyme
                WHERE word = ?
                ORDER BY (kind = 'perfect') DESC, source_count DESC
                LIMIT ?
            """, (word.lower(), limit * 4)).fetchall()
            out = []
            for r in rows:
                cand, syl, kind, sc = r[0], r[1], r[2], r[3]
                if kind != "phrase":
                    j = self.ph.compare(word, cand)
                    if j.kind == "none":
                        continue
                out.append({"word": cand, "syllables": syl, "kind": kind, "source_count": sc})
                if len(out) >= limit:
                    break
            return out
        except (sqlite3.OperationalError, duckdb.Error, Exception):
            return []

    def search_songs(self, query: str = "", search_by: str = "all",
                     genre: str | None = None, year_min: int | None = None,
                     year_max: int | None = None, limit: int = 20,
                     has_midi: bool = False) -> list[SongSummary]:
        """Search the 3.18M Genius lyrics corpus."""
        return self.searcher.search(query=query, search_by=search_by,
                                    genre=genre, year_min=year_min,
                                    year_max=year_max, limit=limit,
                                    has_midi=has_midi)

    def get_song(self, title: str | None = None, artist: str | None = None,
                 song_id: int | None = None) -> SongDetail | None:
        """Fetch complete lyrics and structured sections for a song."""
        return self.searcher.get_song(title=title, artist=artist, song_id=song_id)

    # -- tool 1: analyze a line ---------------------------------------
    def analyze_line(self, text: str, beats: int = 4) -> LineReport:
        st = self.ca.line_stat(text)
        flags = [d for _, _, d in self.ca.prosody_collisions(text, beats=beats)]
        internal = [f"{a}/{b} ({k})"
                    for a, b, k in self.ca.internal_rhymes(text)[:4]]
        return LineReport(
            text=text, syllables=st.syllables, stress_grid=st.stress_grid,
            end_word=st.end_word, unknown_words=st.unknown[:5],
            prosody_flags=flags[:4], internal_rhymes=internal)

    # -- tool 2: find rhymes ------------------------------------------
    def find_rhymes(self, word: str, kinds: tuple[str, ...] = ("perfect", "slant"),
                    min_syllables: int = 1, max_syllables: int | None = None,
                    prefer: str = "fresh", limit: int = 12,
                    min_word_freq: int = 200,
                    mood: str | None = None,
                    valence_target: float | None = None,
                    arousal_target: float | None = None,
                    sensory_modality: str | None = None,
                    min_concreteness: float | None = None,
                    rhyme_ending: str | None = None,
                    rhyme_class: str | None = None,
                    antithetical: bool = False) -> list[RhymeCandidate]:
        """Ranked rhyme candidates.

        A fresh rhyme is a word that IS in the singing vocabulary whose
        PAIRING is uncommon. Ranking on pair_freq alone fails badly: proper
        nouns and obscure entries ('dolder', 'polder', 'nolder' -- all 0 uses
        across 3.18M songs) score as maximally fresh precisely because nobody
        can sing them. min_word_freq gates on corpus word frequency first, so
        candidates are usable before rarity is considered.

        prefer='fresh' -> real words, unusual pairing.
        prefer='close' -> deepest phonetic match first.
        """
        want_perfect = bool({"perfect", "identity"} & set(kinds))
        want_near = bool(set(kinds) - {"perfect", "identity"})

        raw: list[tuple[str, str, int, float]] | None = []
        if want_perfect:
            raw = self._rhymes_sql(word, min_syllables, max_syllables,
                                   min_word_freq, limit * 40)
        if want_near and raw is not None:
            # The indexed path only knows exact rime matches. Near rhymes need
            # the nucleus-based query, or the whole speedup would come at the
            # cost of silently returning zero slant results.
            near = self._near_rhymes_sql(word, min_word_freq, limit * 40)
            if near is None:
                raw = None
            else:
                raw = (raw or []) + near

        # Dictionary sources map: word -> set of sources
        dict_matches = self.dictionary_rhymes(word, limit=limit * 20)
        sources_by_word: dict[str, list[str]] = {}
        for dm in dict_matches:
            sources_by_word.setdefault(dm["word"], []).append(f"{dm['source_count']} source(s)")

        if not raw:
            # No rime_family index hits (or OOV / suffix word): scan CMUdict + suffix fallback
            raw = [(c, j.kind, j.shared_syllables, j.strength)
                   for c, j in self.ph.rhymes(word, kinds=kinds, min_depth=1,
                                              limit=limit * 40)]

        # Also fold in unified dictionary candidates
        seen_words = {c[0] for c in (raw or [])}
        if raw is None:
            raw = []
        for dm in dict_matches:
            dw = dm["word"]
            if dw not in seen_words:
                raw.append((dw, dm["kind"], dm["syllables"] or 1, 0.85))
                seen_words.add(dw)

        out: list[RhymeCandidate] = []
        fallback_out: list[RhymeCandidate] = []
        src_p = self.ph.best(word) or self.ph.resolve(word)
        for cand, kind, depth, strength in raw:
            if kinds and kind not in kinds:
                continue
            p = self.ph.best(cand)
            syl = p.syllables if p else (self.ph.syllables(cand) or 1)
            if syl < min_syllables:
                continue
            if max_syllables and syl > max_syllables:
                continue
            wf = self._word_freq(cand)
            pf = self._pair_freq(word.lower(), cand)
            cand_sources = sources_by_word.get(cand, ["phonetic"])

            cand_p = p or self.ph.resolve(cand)
            if src_p and cand_p:
                cj = self.ph.compare_prons(src_p, cand_p)
                cand_rhyme_class = cj.rhyme_class
                cand_ending = cand_p.rhyme_ending
            else:
                cand_rhyme_class = kind
                cand_ending = "masculine" if syl == 1 else "feminine"

            sens_info = self.ca.get_word_sensory(cand)
            emo_info = self.ca.get_word_emotion(cand)
            conc = sens_info.get("concreteness") if sens_info else None
            dom_mod = sens_info.get("dominant_modality") if sens_info else None
            val = emo_info.get("valence") if emo_info else None
            aro = emo_info.get("arousal") if emo_info else None

            rc = RhymeCandidate(
                word=cand, kind=kind, syllables=syl,
                shared_syllables=depth,
                strength=round(strength, 2),
                corpus_freq=wf, pair_freq=pf,
                verdict=self._verdict(pf),
                sources=cand_sources,
                rhyme_ending=cand_ending,
                rhyme_class=cand_rhyme_class,
                concreteness=conc,
                dominant_modality=dom_mod,
                valence=val,
                arousal=aro)
            fallback_out.append(rc)
            # Gate on usability unless asserted by editorial dictionary or no corpus count
            if wf is not None and wf < min_word_freq and cand not in sources_by_word:
                continue
            out.append(rc)

        if not out and fallback_out:
            out = fallback_out

        has_multi = (
            prefer == "multi_objective"
            or mood is not None
            or valence_target is not None
            or arousal_target is not None
            or sensory_modality is not None
            or min_concreteness is not None
            or rhyme_ending is not None
            or rhyme_class is not None
        )

        if has_multi:
            target_v, target_a = None, None
            if mood:
                m_key = mood.strip().lower()
                if m_key in _MOOD_TARGETS:
                    target_v, target_a = _MOOD_TARGETS[m_key]
            if valence_target is not None:
                target_v = valence_target
            if arousal_target is not None:
                target_a = arousal_target
            if target_v is not None and antithetical:
                target_v = 1.0 - target_v

            for c in out:
                score = c.strength * 35.0 + c.shared_syllables * 5.0
                if c.pair_freq == 0:
                    score += 15.0
                elif c.pair_freq and c.pair_freq < 50:
                    score += 20.0
                elif c.pair_freq and c.pair_freq < 500:
                    score += 10.0
                else:
                    score -= 5.0

                if rhyme_ending:
                    if c.rhyme_ending == rhyme_ending:
                        score += 20.0
                    else:
                        score -= 15.0

                if rhyme_class:
                    if c.rhyme_class == rhyme_class:
                        score += 20.0
                    else:
                        score -= 10.0

                if min_concreteness is not None:
                    if c.concreteness is not None:
                        if c.concreteness >= min_concreteness:
                            score += 20.0 + (c.concreteness - min_concreteness) * 5.0
                        else:
                            score -= 25.0
                    else:
                        score -= 25.0

                if sensory_modality:
                    if c.dominant_modality == sensory_modality:
                        score += 25.0
                    else:
                        score -= 10.0

                if target_v is not None and c.valence is not None:
                    v_diff = abs(c.valence - target_v)
                    a_diff = abs(c.arousal - target_a) if (target_a is not None and c.arousal is not None) else 0.0
                    dist = (v_diff**2 + a_diff**2) ** 0.5
                    score += max(-15.0, 25.0 * (1.0 - dist))

                c.score = round(score, 2)

            out.sort(key=lambda c: -(c.score or 0.0))
        elif prefer == "fresh":
            out.sort(key=lambda c: (_pair_bucket(c.pair_freq),
                                    -c.strength,
                                    -c.shared_syllables,
                                    -(c.corpus_freq or 0)))
        else:
            out.sort(key=lambda c: (-c.shared_syllables, -c.strength,
                                    c.pair_freq or 0))
        return out[:limit]

    def rhyme_sound_options(self, word: str, mood: str | None = None,
                            per_sound: int = 6, min_concreteness: float | None = None
                            ) -> list[dict[str, Any]]:
        """Target rhyme sounds for ``word``, each with ranked example words.

        Returns groups like {"sound": "OW", "label": "Long O (close, rose)",
        "example": "close", "words": ["rose","knows",...]}. Words are drawn from
        find_rhymes (perfect + slant), so they already rhyme with ``word`` and
        are ranked for singability; passing ``mood``/``min_concreteness`` biases
        them toward concrete, tone-aligned choices. Grouped by vowel nucleus so a
        writer can pick 'the Long-O sound' vs 'the short-O sound'.
        """
        cands = self.find_rhymes(
            word, kinds=("perfect", "slant", "family"), limit=250,
            prefer="singable", mood=mood, min_concreteness=min_concreteness,
        )
        # Collect ALL candidates per vowel nucleus, then rank each group so the
        # best examples are common, short, singable words (not rare polysyllables).
        buckets: dict[str, list[RhymeCandidate]] = {}
        for c in cands:
            if c.word.lower() == word.lower():
                continue
            p = self.ph.resolve(c.word)
            nuc = p.nucleus if p and getattr(p, "nucleus", None) else None
            if nuc:
                buckets.setdefault(nuc, []).append(c)
        out: list[dict[str, Any]] = []
        for nuc, cs in buckets.items():
            cs.sort(key=lambda c: (
                -(c.corpus_freq or 0),       # common words first
                c.syllables or 9,            # shorter is more singable
                -(c.strength or 0),
            ))
            words = [c.word for c in cs[:per_sound]]
            if not words:
                continue
            ex = words[0]
            out.append({
                "sound": nuc,
                "label": f"{_VOWEL_LABEL.get(nuc, nuc)} (rhyming with '{ex}')",
                "example": ex,
                "words": words,
                "count": len(cs),
            })
        # Most-populated sounds first: those give the writer the most options.
        out.sort(key=lambda g: -g["count"])
        return out

    # -- tool 3: fit a syllable contour -------------------------------
    def fit_contour(self, syllables: int, rhyme_with: str | None = None,
                    limit: int = 12) -> list[RhymeCandidate]:
        """Words of an exact syllable count, optionally rhyming with a word.

        Used to repair a metric hitch without changing the rhyme.
        """
        if rhyme_with:
            cands = self.find_rhymes(rhyme_with, min_syllables=syllables,
                                     max_syllables=syllables, limit=limit)
            return cands
        db = self.db
        if db is None:
            return []
        try:
            rows = db.execute(
                "SELECT word, corpus_freq FROM lexicon WHERE word_count=1 AND corpus_freq > 0 "
                "ORDER BY corpus_freq DESC LIMIT 4000").fetchall()
        except Exception:
            try:
                rows = db.execute(
                    "SELECT w.normalized, v.freq FROM corpus_vocab v "
                    "JOIN word w ON w.id=v.word_id "
                    "ORDER BY v.freq DESC LIMIT 4000").fetchall()
            except Exception:
                return []
        out: list[RhymeCandidate] = []
        for norm, freq in rows:
            p = self.ph.best(norm)
            if p and p.syllables == syllables:
                out.append(RhymeCandidate(
                    word=norm, kind="contour", syllables=syllables,
                    shared_syllables=0, strength=1.0, corpus_freq=freq,
                    verdict="common" if freq > 5000 else "uncommon"))
            if len(out) >= limit:
                break
        return out

    # -- tool 4: check a draft ----------------------------------------
    def check_draft(self, lines: list[str], kind: str = "verse",
                    target_syllables: int | tuple[int, int] | str | None = None) -> DraftReport:
        clean = [l for l in lines if l.strip()]
        st = self.ca.section_stat(clean, kind=kind)
        issues: list[str] = []

        if st.line_count == 0:
            return DraftReport(0, 0, "", 0.0, 1.0, 0.0, issues=["empty draft"])

        if st.metric_consistency < 0.7:
            issues.append(
                f"ragged meter: syllables range {min(st.contour)}-"
                f"{max(st.contour)} (consistency {st.metric_consistency:.2f})")
        if target_syllables is not None:
            min_syl: int | None = None
            max_syl: int | None = None
            if isinstance(target_syllables, str):
                nums = [int(n) for n in re.findall(r"\d+", target_syllables)]
                if len(nums) >= 2:
                    min_syl, max_syl = min(nums[0], nums[1]), max(nums[0], nums[1])
                elif len(nums) == 1:
                    min_syl = max_syl = nums[0]
            elif isinstance(target_syllables, (tuple, list)) and len(target_syllables) >= 2:
                min_syl, max_syl = int(target_syllables[0]), int(target_syllables[1])
            elif isinstance(target_syllables, (int, float)):
                min_syl = max_syl = int(target_syllables)

            if min_syl is not None and max_syl is not None:
                if min_syl == max_syl:
                    for i, c in enumerate(st.contour):
                        if abs(c - min_syl) > 1:
                            issues.append(
                                f"line {i + 1}: {c} syllables, target {min_syl}")
                else:
                    for i, c in enumerate(st.contour):
                        if c < min_syl:
                            issues.append(
                                f"line {i + 1}: {c} syllables, target {min_syl}-{max_syl} ({min_syl - c} too short)")
                        elif c > max_syl:
                            issues.append(
                                f"line {i + 1}: {c} syllables, target {min_syl}-{max_syl} ({c - max_syl} too long)")
        if st.rhyme_density < 0.5:
            issues.append(f"thin rhyme: only {st.rhyme_density:.0%} of lines rhyme")
        if st.abstract_ratio > 0.12:
            issues.append(
                f"abstract language {st.abstract_ratio:.0%} of words; "
                "ground it in concrete detail")
        if st.sensory_ratio < 0.02 and st.abstract_ratio > 0.05:
            issues.append("no sensory anchors; add a physical image")
        if st.repeat_ratio > 0.4 and kind not in {"chorus", "hook"}:
            issues.append(f"{st.repeat_ratio:.0%} repeated lines in a {kind}")

        # cliche detection from corpus n-grams
        for i, ln in enumerate(clean):
            toks = ln.lower().split()
            for n in (4, 3):
                for s in range(len(toks) - n + 1):
                    gram = " ".join(toks[s:s + n])
                    f = self.phrase_freq(gram)
                    if f and f > 2000:
                        issues.append(
                            f"line {i + 1}: '{gram}' appears {f:,} times in corpus")
                        break
                else:
                    continue
                break

        # predictable end-rhyme pairs
        keys = [self.ca.line_stat(l).end_word for l in clean]
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                a, b = keys[i], keys[j]
                if not a or not b or a == b:
                    continue
                if self.ph.compare(a, b).kind == "perfect":
                    pf = self._pair_freq(a, b)
                    if pf and pf > 500:
                        issues.append(
                            f"lines {i + 1}/{j + 1}: '{a}/{b}' is a stock pair "
                            f"({pf:,} uses)")

        # repeated line opening stems
        if kind not in {"chorus", "hook"}:
            stems: dict[str, list[int]] = {}
            for idx, ln in enumerate(clean):
                words = [w.lower().strip(".,!?;:'\"") for w in ln.split() if w.strip()]
                if len(words) >= 2:
                    stem = " ".join(words[:2])
                    stems.setdefault(stem, []).append(idx + 1)
            for stem, line_nums in stems.items():
                if len(line_nums) >= 3:
                    issues.append(
                        f"repeated line opener '{stem}' on lines {', '.join(str(n) for n in line_nums)}")

        sens_prof = st.sensory_profile
        emo_prof = st.emotion_profile
        dom_mod = sens_prof.dominant_modality if sens_prof else "none"
        mean_c = sens_prof.mean_concreteness if sens_prof else 3.0
        val = emo_prof.valence if emo_prof else 0.5
        aro = emo_prof.arousal if emo_prof else 0.5
        dom_emo = emo_prof.dominant_emotion if emo_prof else "neutral"
        sensory_anchors = [w["word"] for w in (sens_prof.sensory_words if sens_prof else [])[:6]]

        return DraftReport(
            line_count=st.line_count,
            total_syllables=sum(st.contour),
            rhyme_scheme=st.rhyme_scheme,
            rhyme_density=st.rhyme_density,
            metric_consistency=round(st.metric_consistency, 3),
            mean_syllables=st.mean_syllables,
            contour=st.contour,
            abstract_ratio=st.abstract_ratio,
            sensory_ratio=st.sensory_ratio,
            repeat_ratio=st.repeat_ratio,
            readability_grade=self.ca.flesch_kincaid_grade(clean),
            issues=issues[:12],
            dominant_modality=dom_mod,
            mean_concreteness=mean_c,
            valence=val,
            arousal=aro,
            dominant_emotion=dom_emo,
            sensory_anchors=sensory_anchors,
            toggle_pattern=st.toggle_pattern,
            tripod_status=st.tripod_status)

    # -- tool 5: validate a form --------------------------------------
    def validate_form(self, form: str, lines: list[str], **kw) -> dict:
        r = self.fv.validate(form, [l for l in lines if l.strip()], **kw)
        return {
            "form": r.form,
            "ok": r.ok,
            "line_count": r.line_count,
            "violations": [
                {"line": v.line, "rule": v.rule, "detail": v.detail}
                for v in r.violations[:12]
            ],
            "notes": r.notes,
        }

    # -- tool 6: structure reference ----------------------------------
    def section_template(self, kind: str = "chorus", genre: str | None = None,
                         limit: int = 6) -> dict:
        """Observed syllable contours and schemes for a section kind.

        Answers "how long should my chorus lines be" with corpus evidence
        rather than a guess.
        """
        db = self.db
        if db is None:
            return {"kind": kind, "available": False}
        g = genre or "all"
        try:
            contours = db.execute(
                "SELECT contour, freq FROM corpus_contour_stat "
                "WHERE kind=? AND genre=? ORDER BY freq DESC LIMIT ?",
                (kind, g, limit)).fetchall()
            stat_row = db.execute(
                "SELECT avg_line_count, avg_syllables FROM corpus_section_stat WHERE kind=?", (kind,)).fetchone()
            if stat_row:
                stats = stat_row
                schemes = db.execute(
                    "SELECT rhyme_scheme, c FROM corpus_section_scheme WHERE kind=? ORDER BY c DESC LIMIT ?",
                    (kind, limit)).fetchall()
            else:
                schemes = db.execute(
                    "SELECT rhyme_scheme, COUNT(*) c FROM corpus_section "
                    "WHERE kind=? AND rhyme_scheme IS NOT NULL "
                    "GROUP BY rhyme_scheme ORDER BY c DESC LIMIT ?",
                    (kind, limit)).fetchall()
                stats = db.execute(
                    "SELECT ROUND(AVG(line_count),1), ROUND(AVG(mean_syllables),1) "
                    "FROM corpus_section WHERE kind=?", (kind,)).fetchone()
            nxt = db.execute(
                "SELECT to_kind, freq FROM corpus_transition "
                "WHERE from_kind=? AND genre=? ORDER BY freq DESC LIMIT 4",
                (kind, g)).fetchall()
        except Exception:
            return {"kind": kind, "available": False}
        return {
            "kind": kind,
            "genre": g,
            "available": True,
            "avg_lines": stats[0] if stats else None,
            "avg_syllables_per_line": stats[1] if stats else None,
            "common_contours": [{"contour": c, "freq": f} for c, f in contours],
            "common_schemes": [{"scheme": s, "freq": f} for s, f in schemes],
            "usually_followed_by": [{"kind": k, "freq": f} for k, f in nxt],
        }

    def corpus_status(self) -> dict:
        db = self.db
        if db is None:
            return {"available": False}
        out: dict = {"available": True}
        for t in ("corpus_song", "corpus_line", "corpus_vocab", "corpus_ngram",
                  "corpus_rhyme_stat"):
            try:
                out[t] = db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except Exception:
                out[t] = None
        return out

    def analyze_sensory(self, text_or_lines: str | list[str]) -> dict:
        """Sensory and imagery analysis: concreteness score, modalities and sensory anchors."""
        prof = self.ca.analyze_sensory(text_or_lines)
        return {
            "mean_concreteness": prof.mean_concreteness,
            "concrete_ratio": prof.concrete_ratio,
            "abstract_ratio": prof.abstract_ratio,
            "dominant_modality": prof.dominant_modality,
            "modality_scores": prof.modality_scores,
            "sensory_words": prof.sensory_words,
            "abstract_words": prof.abstract_words,
        }

    def analyze_mood(self, text_or_lines: str | list[str]) -> dict:
        """Emotion and mood analysis: VAD scores (valence, arousal, dominance) and emotion distribution."""
        prof = self.ca.analyze_emotion(text_or_lines)
        return {
            "valence": prof.valence,
            "arousal": prof.arousal,
            "dominance": prof.dominance,
            "valence_label": prof.valence_label,
            "arousal_label": prof.arousal_label,
            "dominant_emotion": prof.dominant_emotion,
            "emotions": prof.emotions,
        }

    def mosaic_rhymes(self, word: str, limit: int = 10) -> list[dict]:
        """Multi-word mosaic rhymes with connected speech weak forms."""
        db = self.db
        if db is None:
            return []
        src_p = self.ph.resolve(word)
        if not src_p:
            return []
        try:
            rows = db.execute("""
                SELECT DISTINCT rhyme_word, syllables, source_count
                FROM unified_rhyme
                WHERE word = ? AND kind = 'phrase'
                UNION
                SELECT wp.normalized, wp.word_count, 1
                FROM phrase_substitution ps
                JOIN word wt ON wt.id = ps.target_word_id
                JOIN word wp ON wp.id = ps.phrase_word_id
                WHERE wt.normalized = ? AND wp.word_count > 1
            """, (word.lower(), word.lower())).fetchall()
        except (sqlite3.OperationalError, duckdb.Error, Exception):
            rows = []

        out = []
        seen = set()
        for cand, syl, sc in rows:
            if cand in seen or cand.lower() == word.lower():
                continue
            seen.add(cand)
            mp = self.ph.mosaic_prons(cand) or self.ph.best(cand)
            if not mp:
                continue
            j = self.ph.compare_prons(src_p, mp)
            if j.kind != "none":
                out.append({
                    "phrase": cand,
                    "kind": j.kind,
                    "rhyme_class": j.rhyme_class,
                    "syllables": mp.syllables,
                    "rhyme_ending": mp.rhyme_ending,
                    "strength": j.strength,
                    "sources": sc,
                })
            if len(out) >= limit:
                break
        return out

    def get_section_exemplar(
        self,
        kind: str = "verse",
        song_title: str | None = None,
        artist: str | None = None,
        song_id: int | None = None,
        genre: str | None = None,
        target_syllables: int | tuple[int, int] | str | None = None,
        rhyme_scheme: str | None = None,
        lines_wanted: int = 4,
        exclude_title: str | None = None,
        limit_lines: int = 8,
    ) -> SectionExemplar | None:
        """Retrieve real corpus song section scansion as an exemplar stencil."""
        song = None
        if (song_id or song_title or artist) and (not exclude_title or (song_title and song_title.lower() != exclude_title.lower())):
            song = self.get_song(title=song_title, artist=artist, song_id=song_id)

        # Parse target syllables
        min_syl, max_syl = 8, 14
        if isinstance(target_syllables, (list, tuple)) and len(target_syllables) >= 2:
            min_syl, max_syl = int(target_syllables[0]), int(target_syllables[1])
        elif isinstance(target_syllables, (int, float)):
            min_syl = max_syl = int(target_syllables)
        elif isinstance(target_syllables, str):
            nums = [int(n) for n in re.findall(r"\d+", target_syllables)]
            if len(nums) >= 2:
                min_syl, max_syl = min(nums[0], nums[1]), max(nums[0], nums[1])
            elif len(nums) == 1:
                min_syl = max_syl = nums[0]

        target_scheme = rhyme_scheme.upper() if rhyme_scheme and rhyme_scheme.lower() != "free" else None
        target_lines = lines_wanted or 4

        # Match structurally across the corpus (structure over genre)
        if not song and self.db is not None:
            try:
                query_sql = """
                    SELECT s.title, s.artist, sec.line_count, sec.mean_syllables, sec.syllable_variance
                    FROM corpus_section sec
                    JOIN corpus_song s ON s.id = sec.song_id
                    WHERE sec.kind = ?
                      AND (sec.line_count = ? OR sec.line_count = ?)
                      AND sec.mean_syllables BETWEEN ? AND ?
                      AND sec.syllable_variance < 3.5
                      AND (? IS NULL OR sec.rhyme_scheme = ?)
                      AND (? IS NULL OR lower(s.title) != lower(?))
                    ORDER BY sec.syllable_variance ASC
                    LIMIT 8
                """
                candidates = self.db.execute(
                    query_sql,
                    (kind, target_lines, target_lines * 2, max(1, min_syl - 2), max_syl + 2,
                     target_scheme, target_scheme, exclude_title, exclude_title)
                ).fetchall()

                for c_title, c_artist, _, _, _ in candidates:
                    cand_song = self.get_song(title=c_title, artist=c_artist)
                    if cand_song and any(s.kind == kind for s in cand_song.sections):
                        song = cand_song
                        break
            except Exception:
                song = None

        if not song:
            query = genre or "classic rock"
            matches = self.search_songs(query=query, limit=5)
            for m in matches:
                if exclude_title and m.title.lower() == exclude_title.lower():
                    continue
                candidate_song = self.get_song(song_id=m.song_id, title=m.title, artist=m.artist)
                if candidate_song and any(s.kind == kind for s in candidate_song.sections):
                    song = candidate_song
                    break

        if not song:
            return None
        matching_sections = [s for s in song.sections if s.kind == kind]
        if not matching_sections:
            matching_sections = song.sections
        if not matching_sections:
            return None
        sec = matching_sections[0]
        taken_lines = sec.lines[:limit_lines]
        avg_syl = sum(l.syllables for l in taken_lines) / len(taken_lines) if taken_lines else 0

        # If the corpus lines are split into hemistichs (half-lines under 9 syllables)
        # and we have at least 4 lines, stitch consecutive pairs into full lines with '//'
        if avg_syl <= 8 and len(taken_lines) >= 4 and len(taken_lines) % 2 == 0:
            stitched = []
            for i in range(0, len(taken_lines), 2):
                l1, l2 = taken_lines[i], taken_lines[i+1]
                combo_text = f"{l1.text}, // {l2.text}"
                combo_syl = l1.syllables + l2.syllables
                stitched.append((combo_text, combo_syl, l2.end_word))
            scheme = self.ca._scheme(
                [self.ca.line_stat(end).rime_key for _, _, end in stitched]
            )
            exemplar_lines = [
                ExemplarLine(text=t, syllables=s, end_word=e, rhyme_tag=scheme[i])
                for i, (t, s, e) in enumerate(stitched)
            ]
            return SectionExemplar(
                song_title=song.title,
                artist=song.artist,
                section_name=sec.name,
                kind=sec.kind,
                lines=exemplar_lines,
                rhyme_scheme=scheme,
                contour=[s for _, s, _ in stitched],
            )

        scheme = self.ca._scheme(
            [self.ca.line_stat(l.text).rime_key for l in taken_lines]
        )
        exemplar_lines = []
        for i, l in enumerate(taken_lines):
            tag = scheme[i] if i < len(scheme) and scheme[i] != "-" else ""
            exemplar_lines.append(ExemplarLine(
                text=l.text,
                syllables=l.syllables,
                stress_grid=l.stress_grid,
                end_word=l.end_word,
                rhyme_tag=tag,
            ))
        return SectionExemplar(
            song_title=song.title,
            artist=song.artist,
            section_name=sec.name,
            kind=sec.kind,
            lines=exemplar_lines,
            rhyme_scheme=scheme,
            contour=[l.syllables for l in taken_lines],
        )

    def get_corpus_lyric_passages(
        self,
        reference_title: str | None = None,
        genre: str | None = None,
        limit: int = 4,
    ) -> list[dict[str, Any]]:
        """Retrieve real lyric verses from the 3.18M corpus to provide authentic phrasing exemplars."""
        passages: list[dict[str, Any]] = []
        seen_titles = set()

        if reference_title:
            ref_song = self.get_song(title=reference_title)
            if ref_song and ref_song.sections:
                verse_secs = [s for s in ref_song.sections if s.kind in ("verse", "chorus")]
                sec = verse_secs[0] if verse_secs else ref_song.sections[0]
                lines = [l.text for l in sec.lines[:6]]
                if lines:
                    passages.append({
                        "title": ref_song.title,
                        "artist": ref_song.artist,
                        "section": sec.name,
                        "lines": lines,
                    })
                    seen_titles.add(ref_song.title.lower())

        canonical_queries = ["Hotel California", "Sunflower", "Sultans of Swing", "Paint It Black", "Running on Empty"]
        for q in canonical_queries:
            if len(passages) >= limit:
                break
            if q.lower() in seen_titles:
                continue
            s = self.get_song(title=q)
            if s and s.sections:
                v_secs = [sec for sec in s.sections if sec.kind in ("verse", "chorus")]
                target_sec = v_secs[0] if v_secs else s.sections[0]
                lines = [l.text for l in target_sec.lines[:6]]
                if lines:
                    passages.append({
                        "title": s.title,
                        "artist": s.artist,
                        "section": target_sec.name,
                        "lines": lines,
                    })
                    seen_titles.add(s.title.lower())

        return passages

