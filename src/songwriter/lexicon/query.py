"""Rhyme queries over the DuckDB dictionary.

Every rhyme relation is an equality join on an indexed key column, computed at
read time. Nothing is precomputed, because the five main relations come to ~1.8
billion directed edges on this vocabulary -- alliteration alone is 866M, since
English has only 147 distinct initial onsets. The only stored edges are the
47,472 human-asserted links from the editorial rhyme source, which no phonetic rule reproduces.

Results are capped and ranked before they leave this layer. The consumer is an
LLM that cannot hear, so an uncapped candidate dump both floods its context and
invites it to judge sound itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import duckdb

from songwriter.db import Database

from songwriter.config import CORPUS_DB as DEFAULT_DB


class RhymeType(StrEnum):
    """Every supported relation, with the key each one joins on."""

    PERFECT = "perfect"                      # any depth
    PERFECT_MASCULINE = "perfect_masculine"  # 1 syllable: cat / hat
    PERFECT_FEMININE = "perfect_feminine"    # 2: motion / ocean
    PERFECT_DACTYLIC = "perfect_dactylic"    # 3+: mysterious / delirious
    RICH = "rich"                            # homophones: knight / night
    ASSONANCE = "assonance"                  # same vowels: late / shape
    CONSONANCE = "consonance"                # same consonants: bridge / grudge
    FAMILY = "family"                        # related codas: cat / cap
    PARARHYME = "pararhyme"                  # same frame: flesh / flash
    REVERSE = "reverse"                      # same head: late / lake
    ALLITERATION = "alliteration"            # same onset: bold / bitter
    EYE = "eye"                              # spelling only: cough / rough
    MOSAIC = "mosaic"                        # phrase: dismay / this may
    ADDITIVE = "additive"                    # coda grows: coal / cold
    SUBTRACTIVE = "subtractive"              # coda shrinks: cold / coal
    EDITORIAL = "editorial"                  # human-asserted near rhyme


#: Relations whose SQL is a plain equality join on one key, plus a predicate
#: that keeps the match from collapsing into a stricter relation. Kept as data
#: so the shape of each relation is legible rather than buried in branches.
_JOINS: dict[RhymeType, tuple[str, str]] = {
    RhymeType.PERFECT: ("perfect_tail", ""),
    RhymeType.PERFECT_MASCULINE: ("perfect_tail", "b.perfect_depth = 1"),
    RhymeType.PERFECT_FEMININE: ("perfect_tail", "b.perfect_depth = 2"),
    RhymeType.PERFECT_DACTYLIC: ("perfect_tail", "b.perfect_depth >= 3"),
    RhymeType.RICH: ("phone_key", ""),
    # Assonance and consonance must EXCLUDE perfect rhyme, or every perfect
    # rhyme would also be returned as a weaker slant match.
    RhymeType.ASSONANCE: ("assonance_tail",
                          "b.perfect_tail <> a.perfect_tail"),
    RhymeType.CONSONANCE: ("consonance_tail",
                           "b.assonance_tail <> a.assonance_tail"),
    RhymeType.FAMILY: ("coda_class",
                       "b.assonance_tail = a.assonance_tail "
                       "AND b.perfect_tail <> a.perfect_tail"),
    RhymeType.PARARHYME: ("frame_key", "b.nucleus <> a.nucleus"),
    RhymeType.REVERSE: ("reverse_key",
                        "b.perfect_tail <> a.perfect_tail"),
    RhymeType.ALLITERATION: ("stressed_onset",
                             "b.perfect_tail <> a.perfect_tail"),
    RhymeType.MOSAIC: ("perfect_tail", "wb.is_phrase"),
}


@dataclass(frozen=True)
class Match:
    """One rhyme candidate."""

    word: str
    kind: str
    arpabet: str
    syllables: int
    depth: int
    corpus_freq: int
    end_freq: int
    is_phrase: bool
    confidence: int
    tier: int
    #: Definitions, '[pos] gloss'. Returned with every match because a rhyme
    #: without a meaning cannot be used to write a line -- 'knight' and 'night'
    #: are the same sound and different words.
    glosses: tuple[str, ...] = ()
    #: Parts of speech, for fitting a word to a line's grammar.
    pos: tuple[str, ...] = ()
    #: Register labels, carried as data. They describe; they never filter.
    tags: tuple[str, ...] = ()
    #: Semantic domains ('chess', 'astronomy'), so a rhyme can be picked for the
    #: song's subject and not only for its sound.
    topics: tuple[str, ...] = ()

    @property
    def definition(self) -> str:
        """Shortest useful meaning, for a one-line display."""
        return self.glosses[0] if self.glosses else ""

    @property
    def verdict(self) -> str:
        """How fresh the pairing is, from corpus frequency.

        A word nobody sings is not 'fresh', it is unusable -- so this reports
        commonness of the WORD, and the caller decides.
        """
        if self.corpus_freq >= 100_000:
            return "common"
        if self.corpus_freq >= 10_000:
            return "familiar"
        if self.corpus_freq >= 500:
            return "uncommon"
        return "rare"

    @property
    def is_verified(self) -> bool:
        return self.confidence >= 2


class RhymeDB:
    """Read-only query interface to the rhyme dictionary."""

    def __init__(self, path: Path | str = DEFAULT_DB) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(
                f"{self.path} not built. Run scripts/build_rhymes.py")
        self._db = Database.get(self.path, read_only=True)

    def _connection(self) -> duckdb.DuckDBPyConnection:
        """This thread's cursor on the shared gateway.

        The gateway reopens the file when the builder replaces it on disk, so a
        long-running web or MCP server stays correct across a rebuild.
        """
        return self._db.conn

    def close(self) -> None:
        """No-op: the connection is shared process-wide and owned by the gateway."""

    def __enter__(self) -> RhymeDB:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- lookup -------------------------------------------------------

    def known(self, word: str) -> bool:
        row = self._connection().execute(
            "SELECT 1 FROM word WHERE lemma = ? LIMIT 1",
            [word.lower().strip()]).fetchone()
        return row is not None

    def info(self, word: str) -> dict | None:
        """Everything the dictionary holds about one word."""
        row = self._connection().execute(
            "SELECT lemma, display, is_phrase, word_count, corpus_freq, "
            "       end_freq, song_count, glosses, pos, tags, "
            "       confidence, source_count, spelling_tail, "
            "       homophones, topics, examples, respelling "
            "FROM word WHERE lemma = ?", [word.lower().strip()]).fetchone()
        if row is None:
            return None
        # Word-to-word relations, grouped by relation name. Read from their own
        # table so a relation added upstream appears here without a code change.
        relations: dict[str, list[str]] = {}
        for name, target in self._connection().execute(
            "SELECT relation, target FROM word_relation WHERE lemma = ? "
            "ORDER BY relation, target", [word.lower().strip()]
        ).fetchall():
            relations.setdefault(name, []).append(target)
        prons = self._connection().execute(
            "SELECT p.arpabet, p.syllable_count, p.stress_pattern, p.tier, "
            "       p.note, p.is_variant, k.perfect_tail, k.perfect_depth "
            "FROM word w JOIN pronunciation p ON p.word_id = w.id "
            "LEFT JOIN rhyme_key k ON k.pronunciation_id = p.id "
            "WHERE w.lemma = ? ORDER BY p.tier, p.is_variant",
            [word.lower().strip()]).fetchall()
        return {
            "word": row[0], "display": row[1], "is_phrase": row[2],
            "word_count": row[3], "corpus_freq": row[4], "end_freq": row[5],
            "song_count": row[6],
            "glosses": list(row[7] or ()), "pos": list(row[8] or ()),
            "tags": list(row[9] or ()),
            "confidence": row[10], "source_count": row[11],
            "spelling_tail": row[12],
            # Editorially asserted homophones, and the semantic domains the word
            # belongs to. Sound truth and subject: the two things a phone key
            # cannot supply.
            "homophones": list(row[13] or ()),
            "topics": list(row[14] or ()),
            # A real sentence showing the word in use, and the traditional
            # respelling: independent evidence of syllables and stress.
            "examples": list(row[15] or ()),
            "respelling": row[16] or "",
            # Same meaning/different sound (synonyms), the opposite (antonyms),
            # something more concrete (hyponyms), and the rest.
            "relations": relations,
            "pronunciations": [
                {"arpabet": p[0], "syllables": p[1], "stress": p[2],
                 "tier": p[3], "note": p[4], "is_variant": p[5],
                 "rime": p[6], "depth": p[7]}
                for p in prons],
        }

    def syllables(self, word: str) -> int | None:
        row = self._connection().execute(
            "SELECT p.syllable_count FROM word w "
            "JOIN pronunciation p ON p.word_id = w.id "
            "WHERE w.lemma = ? ORDER BY p.tier, p.is_variant LIMIT 1",
            [word.lower().strip()]).fetchone()
        return row[0] if row else None

    def is_unrhymable(self, word: str) -> bool:
        """Whether the editorial data declares this word unrhymable.

        Checked against the phonetic data too: the assertion that 'orange' has
        no rhyme is about single words, and the database may hold a phrase that
        does rhyme with it.
        """
        row = self._connection().execute(
            "SELECT 1 FROM unrhymable WHERE lemma = ?",
            [word.lower().strip()]).fetchone()
        return row is not None

    # -- the core query -----------------------------------------------

    def rhymes(
        self,
        word: str,
        kinds: tuple[str, ...] = (RhymeType.PERFECT,),
        limit: int = 25,
        min_freq: int = 0,
        max_syllables: int | None = None,
        min_syllables: int | None = None,
        allow_phrases: bool = True,
        exclude_tags: tuple[str, ...] = (),
        require_verified: bool = False,
        prefer: str = "singable",
    ) -> list[Match]:
        """Rhymes of ``word``, ranked and capped.

        ``prefer='singable'`` ranks by line-final corpus frequency, which is
        what actually predicts a usable rhyme -- a word can be common overall
        and never sit at the end of a line. ``prefer='fresh'`` inverts that to
        surface unusual choices, and ``prefer='close'`` ranks by phonetic depth.

        ``exclude_tags`` drops words carrying any of the given register labels.
        One parameter for every label, so no label has a filter of its own: the
        caller passes ``('archaic',)`` or ``('vulgar',)`` through the same door
        and the engine treats them identically.
        """
        target = word.lower().strip()
        results: dict[str, Match] = {}

        for kind in kinds:
            for match in self._one_kind(
                target, RhymeType(kind), limit * 4, min_freq, max_syllables,
                min_syllables, allow_phrases, tuple(exclude_tags),
                require_verified,
            ):
                # A word may satisfy several relations; keep the strongest,
                # which is the order `kinds` was given in.
                if match.word not in results:
                    results[match.word] = match

        kind_rank = {k: i for i, k in enumerate(kinds)}
        ordered = list(results.values())
        if prefer == "fresh":
            ordered.sort(key=lambda m: (kind_rank.get(m.kind, 99), m.corpus_freq, -m.depth))
        elif prefer == "close":
            ordered.sort(key=lambda m: (kind_rank.get(m.kind, 99), -m.depth, -m.end_freq))
        else:
            ordered.sort(key=lambda m: (kind_rank.get(m.kind, 99), -m.end_freq, -m.corpus_freq, -m.depth))
        return ordered[:limit]

    def _one_kind(
        self, target: str, kind: RhymeType, limit: int, min_freq: int,
        max_syllables: int | None, min_syllables: int | None,
        allow_phrases: bool, exclude_tags: tuple[str, ...],
        require_verified: bool,
    ) -> list[Match]:
        if kind is RhymeType.EYE:
            return self._eye(target, limit, min_freq)
        if kind in {RhymeType.ADDITIVE, RhymeType.SUBTRACTIVE}:
            return self._coda_change(target, kind, limit, min_freq)
        if kind is RhymeType.EDITORIAL:
            return self._editorial(target, limit, min_freq)

        column, extra = _JOINS[kind]
        filters = [
            "wb.lemma <> wa.lemma",
            f"b.{column} IS NOT NULL",
            "wb.corpus_freq >= ?",
        ]
        params: list[object] = [target, min_freq]
        if extra:
            filters.append(extra)
        if not allow_phrases and kind is not RhymeType.MOSAIC:
            filters.append("NOT wb.is_phrase")
        # One filter for every register label. 'archaic' and 'vulgar' take the
        # same path with the same SQL; neither has handling of its own.
        for tag in exclude_tags:
            filters.append("NOT list_contains(wb.tags, ?)")
            params.append(tag)
        if require_verified:
            filters.append("wb.confidence >= 2")
        if max_syllables is not None:
            filters.append("pb.syllable_count <= ?")
            params.append(max_syllables)
        if min_syllables is not None:
            filters.append("pb.syllable_count >= ?")
            params.append(min_syllables)

        # RICH rhyme is identical phones, which for a different word means a
        # homophone. Every other relation must not return an identical
        # pronunciation, or 'night' would be offered as a slant rhyme for
        # 'knight'.
        if kind is not RhymeType.RICH:
            filters.append("b.phone_key <> a.phone_key")

        sql = f"""
            SELECT wb.lemma, pb.arpabet, pb.syllable_count, b.perfect_depth,
                   wb.corpus_freq, wb.end_freq, wb.is_phrase,
                   wb.confidence, pb.tier, wb.glosses, wb.pos, wb.tags,
                   wb.topics
            FROM word wa
            JOIN rhyme_key a ON a.word_id = wa.id
            JOIN rhyme_key b ON b.{column} = a.{column}
            JOIN word wb ON wb.id = b.word_id
            JOIN pronunciation pb ON pb.id = b.pronunciation_id
            WHERE wa.lemma = ? AND {' AND '.join(filters)}
            ORDER BY wb.end_freq DESC, wb.corpus_freq DESC
            LIMIT {int(limit)}
        """
        rows = self._connection().execute(sql, params).fetchall()
        return [self._match(kind, r) for r in rows]

    def _eye(self, target: str, limit: int, min_freq: int) -> list[Match]:
        """Eye rhyme: looks alike, sounds different ('cough'/'bough').

        Requires a 3-character shared tail. Two characters would pair every
        word ending in 'ed' and return noise.
        """
        rows = self._connection().execute("""
            SELECT wb.lemma, pb.arpabet, pb.syllable_count, b.perfect_depth,
                   wb.corpus_freq, wb.end_freq, wb.is_phrase,
                   wb.confidence, pb.tier, wb.glosses, wb.pos, wb.tags,
                   wb.topics
            FROM word wa
            JOIN rhyme_key a ON a.word_id = wa.id
            JOIN word wb ON wb.spelling_tail = wa.spelling_tail
            JOIN rhyme_key b ON b.word_id = wb.id
            JOIN pronunciation pb ON pb.id = b.pronunciation_id
            WHERE wa.lemma = ? AND wb.lemma <> wa.lemma
              AND length(wa.spelling_tail) >= 3
              AND b.perfect_tail <> a.perfect_tail
              AND wb.corpus_freq >= ?
            ORDER BY wb.end_freq DESC LIMIT ?
        """, [target, min_freq, limit]).fetchall()
        return [self._match(RhymeType.EYE, r) for r in rows]

    def _coda_change(self, target: str, kind: RhymeType, limit: int,
                     min_freq: int) -> list[Match]:
        """Additive and subtractive rhyme: 'coal' -> 'cold' and back.

        Not an equality join. The nucleus must match while one coda extends the
        other, which is a prefix test on the coda string.
        """
        if kind is RhymeType.ADDITIVE:
            coda_test = ("b.coda <> a.coda AND b.coda LIKE a.coda || '%'")
        else:
            coda_test = ("b.coda <> a.coda AND a.coda LIKE b.coda || '%'")

        rows = self._connection().execute(f"""
            WITH anchors AS (
                SELECT s.pronunciation_id, s.nucleus, s.coda, k.word_id,
                       k.perfect_tail
                FROM syllable s
                JOIN rhyme_key k ON k.pronunciation_id = s.pronunciation_id
                WHERE s.is_anchor
            )
            SELECT wb.lemma, pb.arpabet, pb.syllable_count, kb.perfect_depth,
                   wb.corpus_freq, wb.end_freq, wb.is_phrase,
                   wb.confidence, pb.tier, wb.glosses, wb.pos, wb.tags,
                   wb.topics
            FROM word wa
            JOIN anchors a ON a.word_id = wa.id
            JOIN anchors b ON b.nucleus = a.nucleus AND {coda_test}
            JOIN word wb ON wb.id = b.word_id
            JOIN rhyme_key kb ON kb.pronunciation_id = b.pronunciation_id
            JOIN pronunciation pb ON pb.id = b.pronunciation_id
            WHERE wa.lemma = ? AND wb.lemma <> wa.lemma
              AND wb.corpus_freq >= ?
            ORDER BY wb.end_freq DESC LIMIT ?
        """, [target, min_freq, limit]).fetchall()
        return [self._match(kind, r) for r in rows]

    def _editorial(self, target: str, limit: int,
                   min_freq: int) -> list[Match]:
        """Near rhymes a human editor asserted, from the editorial rhyme source.

        The one relation with no computational substitute: these link rime
        families that phonetics says are unrelated but a working lyricist
        treats as rhymes.
        """
        rows = self._connection().execute("""
            SELECT wb.lemma, pb.arpabet, pb.syllable_count, b.perfect_depth,
                   wb.corpus_freq, wb.end_freq, wb.is_phrase,
                   wb.confidence, pb.tier, wb.glosses, wb.pos, wb.tags,
                   wb.topics
            FROM word wa
            JOIN rhyme_key a ON a.word_id = wa.id
            JOIN editorial_bridge eb ON eb.from_tail = a.perfect_tail
            JOIN rhyme_key b ON b.perfect_tail = eb.to_tail
            JOIN word wb ON wb.id = b.word_id
            JOIN pronunciation pb ON pb.id = b.pronunciation_id
            WHERE wa.lemma = ? AND wb.lemma <> wa.lemma
              AND wb.corpus_freq >= ?
            ORDER BY eb.weight DESC, wb.end_freq DESC LIMIT ?
        """, [target, min_freq, limit]).fetchall()
        return [self._match(RhymeType.EDITORIAL, r) for r in rows]

    @staticmethod
    def _match(kind: RhymeType, row: tuple) -> Match:
        """Build a Match from a row.

        Every query in this module selects the same columns in the same order,
        so one constructor serves all of them.
        """
        return Match(
            word=row[0], kind=str(kind), arpabet=row[1], syllables=row[2],
            depth=row[3], corpus_freq=row[4], end_freq=row[5],
            is_phrase=row[6], confidence=row[7], tier=row[8],
            glosses=tuple(row[9] or ()), pos=tuple(row[10] or ()),
            tags=tuple(row[11] or ()), topics=tuple(row[12] or ()),
        )

    # -- helpers for the drafting layer -------------------------------

    def words_with_syllables(self, count: int, limit: int = 25,
                             min_freq: int = 500,
                             rhymes_with: str | None = None) -> list[Match]:
        """Words of an exact syllable count, for repairing a metric hitch."""
        if rhymes_with:
            return self.rhymes(rhymes_with, limit=limit, min_freq=min_freq,
                               min_syllables=count, max_syllables=count)
        rows = self._connection().execute("""
            SELECT w.lemma, p.arpabet, p.syllable_count, k.perfect_depth,
                   w.corpus_freq, w.end_freq, w.is_phrase,
                   w.confidence, p.tier, w.glosses, w.pos, w.tags,
                   w.topics
            FROM word w
            JOIN pronunciation p ON p.word_id = w.id
            JOIN rhyme_key k ON k.pronunciation_id = p.id
            WHERE p.syllable_count = ? AND w.corpus_freq >= ?
              AND NOT p.is_variant
            ORDER BY w.end_freq DESC, w.corpus_freq DESC LIMIT ?
        """, [count, min_freq, limit]).fetchall()
        return [self._match(RhymeType.PERFECT, r) for r in rows]

    def stats(self) -> dict[str, int]:
        return dict(self._connection().execute(
            "SELECT name, value FROM build_stat").fetchall())

    def checks(self) -> list[tuple[str, bool, int, str, str]]:
        """The build's own verification results, carried in the database."""
        return self._connection().execute(
            "SELECT name, passed, observed, expected, detail "
            "FROM build_check ORDER BY passed, name").fetchall()
