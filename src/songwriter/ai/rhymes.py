"""RhymeService: the one place generation asks for rhymes.

Merges the two sources the line-writer needs -- the rhyme dictionary
(``RhymeDB.rhymes``, ranked by line-final frequency) and the workbench's
curated / mood-ranked candidates (``Workbench.find_rhymes``) -- applies the
song's register filters and banned pairs, and caches the result per
(word, filter settings). A retry, a repeated chorus, or a second couplet on
the same sound costs no database round-trip.

All reads go through the shared read-only DB gateway.
"""

from __future__ import annotations

import re
import threading
from typing import Any

from songwriter.craft.stats import TRAFFIC_DIRECTOR_WORDS

from songwriter.ai.generation import SongGeneration

_PROFANITY = frozenset({"ass", "shit", "fuck", "bitch", "cunt", "dick", "cock", "damn"})
_PROFANITY_EXEMPT = frozenset({"bass", "class", "grass", "pass", "mass", "brass", "compass", "morass"})
# Words the dictionary returns that make weak line-final rhymes.
_STOP_RHYMES = frozenset({"all", "y'all", "at all", "not at all", "of all", "it all"})
# Below this many perfect rhymes, near-rhymes are appended so the writer still
# has options.
_MIN_TRUE_RHYMES = 6
# A rhyme counts as "common" when it ends at least this many corpus lines.
_COMMON_END_FREQ = 500
# A line that a later line must rhyme with needs at least this many common
# rhymes, or the partner line is forced onto an awkward word.
MIN_ANCHOR_FERTILITY = 7
_VOWEL = re.compile(r"[aeiouy]")
# Derogatory slang the dictionary leaves untagged; never offered radio-clean.
_SLURS = frozenset({"ho", "hoe", "hos", "hoes", "thot", "skank", "slut", "whore"})


def _weak_landing(workbench: Any, word: str) -> bool:
    """True when a word's rhyme vowel is not its main stress (to-MOR-row)."""
    try:
        p = workbench.ph.resolve(word)
    except Exception:
        return False
    if not p or not p.vowels:
        return False
    v = p.vowels[p.anchor_index]
    return not v.endswith("1") and "1" in p.stress_pattern


class RhymeService:
    def __init__(self, workbench: Any, rhyme_db: Any | None = None) -> None:
        self._wb = workbench
        if rhyme_db is None:
            try:
                from songwriter.lexicon.query import RhymeDB
                rhyme_db = RhymeDB()
            except Exception:
                rhyme_db = None
        self._rdb = rhyme_db
        self._cache: dict[tuple, list[str]] = {}
        # Line-final corpus frequency per candidate, recorded during queries.
        self._end_freq: dict[str, int] = {}
        self._lock = threading.Lock()
        self.lookups = 0  # database round-trips, for tests and analytics

    @staticmethod
    def _filter_key(gen: SongGeneration, mood: str | None) -> tuple:
        return (gen.allow_slang, gen.allow_offensive, gen.allow_phrases, mood)

    def rhymes(self, word: str, gen: SongGeneration, limit: int = 30) -> list[str]:
        """Ranked, filtered rhyme candidates for ``word`` under ``gen``'s settings."""
        w = (word or "").strip().lower()
        if not w:
            return []
        mood = gen.answers.get("target_mood") or None
        key = (w, self._filter_key(gen, mood))
        with self._lock:
            cached = self._cache.get(key)
        if cached is None:
            cached = self._query(w, gen, mood)
            with self._lock:
                self._cache[key] = cached
        banned_partners = self._banned_partners(w, gen)
        return [c for c in cached if c.lower() not in banned_partners][:limit]

    def prefetch(self, words: list[str], gen: SongGeneration) -> None:
        """Warm the cache for seed words before drafting starts."""
        for w in words:
            self.rhymes(w, gen)

    def fertility(self, word: str, gen: SongGeneration) -> int:
        """How many COMMON true rhymes ``word`` offers after the song's filters.

        Counts candidates that end lines at least ``_COMMON_END_FREQ`` times in
        the corpus. Words with a broad singable candidate pool (>= 40 rhymes) and
        at least 5 common endings (e.g. 'proud' with 70+ rhymes) also pass, while
        genuinely starved words ('rumble', 'purple') score < 7.
        """
        cands = self.rhymes(word, gen, limit=80)
        high_freq = sum(1 for c in cands if self._end_freq.get(c.lower(), 0) >= _COMMON_END_FREQ)
        if high_freq >= 5 and len(cands) >= 40:
            return max(MIN_ANCHOR_FERTILITY, high_freq)
        return high_freq
    @staticmethod
    def _banned_partners(word: str, gen: SongGeneration) -> set[str]:
        out: set[str] = set()
        for pair in gen.banned_pairs:
            parts = [p.strip().lower() for p in pair.split("/")]
            if len(parts) == 2 and word in parts:
                out.add(parts[1] if parts[0] == word else parts[0])
        return out

    def _query(self, w: str, gen: SongGeneration, mood: str | None) -> list[str]:
        self.lookups += 1
        exclude: list[str] = []
        if not gen.allow_offensive:
            exclude += ["offensive", "vulgar"]
        if not gen.allow_slang:
            exclude.append("slang")

        seen = {w}
        out: list[str] = []

        target_primary = not _weak_landing(self._wb, w)

        def add(candidate: str) -> None:
            c = candidate.strip()
            cl = c.lower()
            # Exclude identical word, exact inflection, or compound ending with w
            is_same = (cl == w) or (cl in (w + "s", w + "es", w + "d", w + "ed", w + "ing"))
            is_compound = False
            if cl.endswith(w) and len(cl) > len(w):
                pfx = cl[:-len(w)]
                common_pfx = ("mid", "day", "sun", "moon", "under", "over", "out", "back", "fore", "down", "up", "after", "good")
                if pfx in common_pfx or (len(pfx) >= 4 and len(w) >= 4):
                    is_compound = True
            if not c or cl in seen or cl in _STOP_RHYMES or is_same or is_compound:
                return
            # Abbreviations and fragments ('st', 'ft', 'e') are not singable
            # words, and the prompt forbids rhyming on function words.
            if len(cl) < 2 or not _VOWEL.search(cl) or cl in TRAFFIC_DIRECTOR_WORDS:
                return
            if not gen.allow_offensive and ((any(b in cl for b in _PROFANITY) and cl not in _PROFANITY_EXEMPT)
                                            or cl in _SLURS):
                return
            # True rhyme lands on a stressed beat: 'tomorrow' stresses MOR, so its
            # weak final 'ow' is not a true rhyme for 'throw'.
            if target_primary and _weak_landing(self._wb, cl):
                return
            if not gen.allow_phrases and " " in cl:
                return
            seen.add(cl)
            out.append(c)

        # The prompt demands true rhyme (vowel + every following consonant), so
        # perfect matches lead. Family/additive near-rhymes (space -> safe,
        # taste) are appended only when perfect rhymes are too scarce to write
        # with; the workbench's curated list is admitted only where it agrees
        # with the dictionary's perfect set.
        perfect: list[str] = []
        near: list[str] = []
        if self._rdb is not None:
            try:
                for m in self._rdb.rhymes(
                    w,
                    kinds=("perfect", "family", "additive"),
                    limit=80,
                    allow_phrases=gen.allow_phrases,
                    exclude_tags=tuple(exclude),
                    prefer="singable",
                ):
                    (perfect if str(m.kind).startswith("perfect") else near).append(m.word)
                    self._end_freq[m.word.lower()] = int(m.end_freq or 0)
            except Exception:
                pass
        # Dictionary perfects keep their singable ranking (face, place, race...).
        # The workbench's curated list is freshness-ranked and leads with rare
        # compounds (suitcase, scarface), so it is only a fallback source.
        for p in perfect:
            add(p)
        if self._rdb is None:
            try:
                for r in self._wb.find_rhymes(w, limit=40, mood=mood):
                    add(r.word)
            except Exception:
                pass
        elif len(out) < _MIN_TRUE_RHYMES:
            for n in near:
                add(n)
        return out
