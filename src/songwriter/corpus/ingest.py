"""Corpus ingestion: lyrics parquet -> SQLite.

Source: MIT-licensed lyrics corpus, 3,179,588 songs across 10
parquet shards, columns title/artist/tag/year/lyrics.

Stores line text plus computed phonetic shape, and derives the aggregate
statistics that make craft checks empirical rather than guessed:

  corpus_ngram        -- phrase frequency; high freq == cliche
  corpus_rhyme_stat   -- how often a rhyme pair actually occurs; the
                         denominator for surprise_score
  corpus_contour_stat -- observed syllable contours per section kind
  corpus_transition   -- observed section orderings

Performance notes for the full pass (~235M lines):
  * primary keys are assigned in-process so every insert can be batched
  * indexes are dropped during bulk load and rebuilt after
  * unbounded counters are pruned by escalating min-frequency, which keeps
    heavy hitters (exactly what cliche detection needs) and drops the tail
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow.parquet as pq

from songwriter.phonetics import Phonetics

_SECTION_MAP = {
    "intro": "intro",
    "verse": "verse",
    "pre-chorus": "pre_chorus",
    "prechorus": "pre_chorus",
    "pre chorus": "pre_chorus",
    "pre-hook": "pre_chorus",
    "chorus": "chorus",
    "post-chorus": "post_chorus",
    "postchorus": "post_chorus",
    "post chorus": "post_chorus",
    "bridge": "bridge",
    "hook": "hook",
    "refrain": "refrain",
    "outro": "outro",
    "interlude": "interlude",
    "breakdown": "breakdown",
    "drop": "drop",
    "solo": "solo",
    "instrumental": "instrumental",
}

_BRACKET = re.compile(r"^\[([^\]]{1,80})\]\s*$", re.M)
_TOKEN = re.compile(r"[a-z']+")

NGRAM_SIZES = (2, 3, 4, 5)
_NGRAM_CAP = 6_000_000
_RHYME_CAP = 4_000_000
_CONTOUR_CAP = 2_000_000


def normalize_section(raw: str) -> str:
    """Bracket label -> canonical section kind.

    Genius brackets double as speaker labels in musicals and posse cuts
    ([Hamilton], [Burr]) and carry featured artists ([Verse 2: Drake]).
    Unrecognised labels become 'other' rather than inventing a section type.
    """
    s = raw.lower().strip()
    s = s.split(":")[0].strip()
    s = re.sub(r"\s*\d+\s*$", "", s)
    s = re.sub(r"[_\s]+", " ", s).strip()
    return _SECTION_MAP.get(s, "other")


@dataclass
class SectionStruct:
    kind: str
    ordinal: int
    lines: list[str] = field(default_factory=list)


def split_sections(lyrics: str) -> list[SectionStruct]:
    """Split lyric text into labelled sections."""
    if not lyrics:
        return []
    matches = list(_BRACKET.finditer(lyrics))
    if not matches:
        body = [l.strip() for l in lyrics.split("\n") if l.strip()]
        return [SectionStruct("unlabelled", 0, body)] if body else []

    out: list[SectionStruct] = []
    ordinal = 0
    pre = [l.strip() for l in lyrics[: matches[0].start()].split("\n") if l.strip()]
    if pre:
        out.append(SectionStruct("unlabelled", ordinal, pre))
        ordinal += 1

    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(lyrics)
        body = [l.strip() for l in lyrics[m.end():end].split("\n") if l.strip()]
        if body:
            out.append(SectionStruct(normalize_section(m.group(1)), ordinal, body))
            ordinal += 1
    return out


def rhyme_scheme(end_keys: list[str | None], max_letters: int = 8) -> str:
    """Scheme letters from end-rhyme keys. '-' = no or unknown rhyme."""
    labels: dict[str, str] = {}
    out: list[str] = []
    nxt = 0
    for k in end_keys:
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


class TokenCache:
    """Memoised per-token phonetics. Tokens recur constantly across the
    corpus, so this turns most lookups into a dict hit.
    """

    __slots__ = ("ph", "_syl", "_stress", "_rime")

    def __init__(self, ph: Phonetics) -> None:
        self.ph = ph
        self._syl: dict[str, int] = {}
        self._stress: dict[str, str] = {}
        self._rime: dict[str, str | None] = {}

    def syllables(self, tok: str) -> int:
        v = self._syl.get(tok)
        if v is None:
            p = self.ph.best(tok)
            v = p.syllables if p else 0
            self._syl[tok] = v
        return v

    def stress(self, tok: str) -> str:
        v = self._stress.get(tok)
        if v is None:
            p = self.ph.best(tok)
            v = p.stress_pattern if p else ""
            self._stress[tok] = v
        return v

    def rime(self, tok: str) -> str | None:
        if tok in self._rime:
            return self._rime[tok]
        p = self.ph.best(tok)
        v = p.rime_stressed() if p else None
        self._rime[tok] = v
        return v


def _prune(counter: Counter, cap: int, state: list[int]) -> None:
    """Bound memory by dropping the low-frequency tail.

    Escalates the threshold so repeated pruning keeps working. Lossy for
    mid-frequency entries by design: cliche detection cares about the top.
    """
    if len(counter) <= cap:
        return
    thr = state[0]
    while len(counter) > cap * 0.7:
        for k in [k for k, v in counter.items() if v <= thr]:
            del counter[k]
        thr += 1
        if thr > 50:
            break
    state[0] = thr


class CorpusIngester:
    def __init__(self, db: sqlite3.Connection, ph: Phonetics,
                 store_lines: bool = True) -> None:
        self.db = db
        self.tc = TokenCache(ph)
        self.store_lines = store_lines
        self.source_id = self._ensure_source()

        # in-process id assignment so all inserts can be batched
        self.next_song = self._max("corpus_song") + 1
        self.next_section = self._max("corpus_section") + 1
        self.next_line = self._max("corpus_line") + 1
        self.next_word = self._max("word") + 1

        self.word_ids: dict[str, int] = {
            r[0]: r[1] for r in db.execute(
                "SELECT normalized, id FROM word WHERE word_count=1")
        }

        # vocab: token -> [freq, songs, end, chorus, first_year, last_year]
        self.vocab: dict[str, list] = {}
        self.ngrams: Counter[tuple[str, int]] = Counter()
        self.rhyme_stat: Counter[tuple[str, str]] = Counter()
        self.contours: Counter[tuple[str, str, str]] = Counter()
        self.transitions: Counter[tuple[str, str, str]] = Counter()
        self._ng_thr = [1]
        self._rs_thr = [1]
        self._ct_thr = [1]

        self._songs: list[tuple] = []
        self._sections: list[tuple] = []
        self._lines: list[tuple] = []
        self._new_words: list[tuple] = []
        self.lines_kept = 0

    def _max(self, table: str) -> int:
        try:
            r = self.db.execute(f"SELECT COALESCE(MAX(id),0) FROM {table}").fetchone()
            return r[0] or 0
        except sqlite3.OperationalError:
            return 0

    def _ensure_source(self) -> int:
        self.db.execute(
            "INSERT OR IGNORE INTO source(slug,filename,title,accent,kind,notes) "
            "VALUES(?,?,?,?,?,?)",
            ("genius-cleaned", "data/raw/*.parquet",
             "lyrics corpus", "en-US", "corpus",
             "MIT licensed. 3,179,588 songs."),
        )
        return self.db.execute(
            "SELECT id FROM source WHERE slug='genius-cleaned'").fetchone()[0]

    def word_id(self, tok: str) -> int:
        wid = self.word_ids.get(tok)
        if wid is None:
            wid = self.next_word
            self.next_word += 1
            self.word_ids[tok] = wid
            self._new_words.append((wid, tok, tok, 0, 1))
        return wid

    # -- per song -----------------------------------------------------
    def ingest_song(self, title, artist, genre, year, lyrics) -> bool:
        sections = split_sections(lyrics)
        if not sections:
            return False

        genre = genre or "unknown"
        song_id = self.next_song
        all_lines = all_words = 0
        seen: set[str] = set()
        seq: list[str] = []
        sec_rows: list[tuple] = []
        line_rows: list[tuple] = []
        body_tokens: set[str] = set()

        for sec in sections:
            sec_id = self.next_section
            contour: list[int] = []
            end_keys: list[str | None] = []
            end_toks: list[str | None] = []
            ln_ord = 0

            for raw in sec.lines:
                toks = _TOKEN.findall(raw.lower())
                if not toks:
                    continue
                all_lines += 1
                all_words += len(toks)
                body_tokens.update(toks)
                norm = " ".join(toks)
                is_rep = 1 if norm in seen else 0
                seen.add(norm)

                syl = 0
                grid: list[str] = []
                for t in toks:
                    syl += self.tc.syllables(t)
                    grid.append(self.tc.stress(t))
                    v = self.vocab.get(t)
                    if v is None:
                        v = [0, 0, 0, 0, year, year]
                        self.vocab[t] = v
                    v[0] += 1
                    if sec.kind == "chorus":
                        v[3] += 1
                    if year:
                        v[4] = year if v[4] is None else min(v[4], year)
                        v[5] = year if v[5] is None else max(v[5], year)

                last = toks[-1]
                self.vocab[last][2] += 1
                rk = self.tc.rime(last)
                end_keys.append(rk)
                end_toks.append(last)
                contour.append(syl)

                if self.store_lines:
                    line_rows.append((
                        self.next_line, sec_id, song_id, ln_ord, raw, syl,
                        "".join(grid), self.word_id(last), rk, len(toks), is_rep,
                    ))
                    self.next_line += 1
                ln_ord += 1

                for n in NGRAM_SIZES:
                    if len(toks) >= n:
                        for i in range(len(toks) - n + 1):
                            self.ngrams[(" ".join(toks[i:i + n]), n)] += 1

            if not contour:
                continue

            for i in range(len(end_keys)):
                for j in (i + 1, i + 2):
                    if j < len(end_keys):
                        ka, kb = end_keys[i], end_keys[j]
                        wa, wb = end_toks[i], end_toks[j]
                        if ka and ka == kb and wa != wb:
                            self.rhyme_stat[(wa, wb) if wa < wb else (wb, wa)] += 1

            mean = sum(contour) / len(contour)
            var = sum((c - mean) ** 2 for c in contour) / len(contour)
            csv = ",".join(map(str, contour))
            sec_rows.append((sec_id, song_id, sec.kind, sec.ordinal, len(contour),
                             csv, rhyme_scheme(end_keys), round(mean, 2),
                             round(var, 2)))
            if len(contour) <= 16:
                self.contours[(sec.kind, csv, genre)] += 1
                self.contours[(sec.kind, csv, "all")] += 1
            seq.append(sec.kind)
            self.next_section += 1

        if not sec_rows:
            return False

        for a, b in zip(seq, seq[1:]):
            self.transitions[(a, b, genre)] += 1
            self.transitions[(a, b, "all")] += 1

        for t in body_tokens:
            self.vocab[t][1] += 1

        rep = len(seen) / all_lines if all_lines else None
        self._songs.append((song_id, title, artist, genre, year, self.source_id,
                            len(sec_rows), all_lines, all_words, ",".join(seq),
                            int("bridge" in seq),
                            round(rep, 4) if rep else None))
        self._sections.extend(sec_rows)
        self._lines.extend(line_rows)
        self.lines_kept += len(line_rows)
        self.next_song += 1

        if len(self.ngrams) > _NGRAM_CAP:
            _prune(self.ngrams, _NGRAM_CAP, self._ng_thr)
        if len(self.rhyme_stat) > _RHYME_CAP:
            _prune(self.rhyme_stat, _RHYME_CAP, self._rs_thr)
        if len(self.contours) > _CONTOUR_CAP:
            _prune(self.contours, _CONTOUR_CAP, self._ct_thr)
        return True

    # -- batching -----------------------------------------------------
    def flush_rows(self) -> None:
        if self._new_words:
            self.db.executemany(
                "INSERT OR IGNORE INTO word(id,surface,normalized,is_phrase,"
                "word_count) VALUES(?,?,?,?,?)", self._new_words)
            self._new_words.clear()
        if self._songs:
            self.db.executemany(
                "INSERT INTO corpus_song(id,title,artist,genre,year,source_id,"
                "section_count,line_count,word_count,section_sequence,"
                "has_bridge,repetition_ratio) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                self._songs)
            self._songs.clear()
        if self._sections:
            self.db.executemany(
                "INSERT INTO corpus_section(id,song_id,kind,ordinal,line_count,"
                "syllable_contour,rhyme_scheme,mean_syllables,syllable_variance)"
                " VALUES(?,?,?,?,?,?,?,?,?)", self._sections)
            self._sections.clear()
        if self._lines:
            self.db.executemany(
                "INSERT INTO corpus_line(id,section_id,song_id,ordinal,text,"
                "syllables,stress_grid,end_word_id,rime_key,word_count,"
                "is_repeat) VALUES(?,?,?,?,?,?,?,?,?,?,?)", self._lines)
            self._lines.clear()
        self.db.commit()

    def flush_aggregates(self, min_freq: int = 3) -> dict[str, int]:
        self.flush_rows()
        rows = []
        for tok, v in self.vocab.items():
            if v[0] < min_freq:
                continue
            rows.append((self.word_id(tok), v[0], v[1], v[2], v[3], v[4], v[5]))
        if self._new_words:
            self.db.executemany(
                "INSERT OR IGNORE INTO word(id,surface,normalized,is_phrase,"
                "word_count) VALUES(?,?,?,?,?)", self._new_words)
            self._new_words.clear()
        self.db.executemany(
            "INSERT INTO corpus_vocab(word_id,freq,song_count,end_freq,"
            "chorus_freq,first_year,last_year) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(word_id) DO UPDATE SET freq=freq+excluded.freq,"
            "song_count=song_count+excluded.song_count,"
            "end_freq=end_freq+excluded.end_freq,"
            "chorus_freq=chorus_freq+excluded.chorus_freq", rows)

        ng = [(g, n, f, f) for (g, n), f in self.ngrams.items() if f >= min_freq]
        self.db.executemany(
            "INSERT INTO corpus_ngram(gram,n,freq,song_count) VALUES(?,?,?,?) "
            "ON CONFLICT(gram,n) DO UPDATE SET freq=freq+excluded.freq", ng)

        rs = []
        for (a, b), f in self.rhyme_stat.items():
            if f >= min_freq:
                rs.append((self.word_id(a), self.word_id(b), f, f))
        if self._new_words:
            self.db.executemany(
                "INSERT OR IGNORE INTO word(id,surface,normalized,is_phrase,"
                "word_count) VALUES(?,?,?,?,?)", self._new_words)
            self._new_words.clear()
        self.db.executemany(
            "INSERT INTO corpus_rhyme_stat(word_a,word_b,pair_freq,song_count) "
            "VALUES(?,?,?,?) ON CONFLICT(word_a,word_b) DO UPDATE SET "
            "pair_freq=pair_freq+excluded.pair_freq", rs)

        self.db.executemany(
            "INSERT INTO corpus_contour_stat(kind,contour,genre,freq) "
            "VALUES(?,?,?,?) ON CONFLICT(kind,contour,genre) DO UPDATE SET "
            "freq=freq+excluded.freq",
            [(k, c, g, f) for (k, c, g), f in self.contours.items()
             if f >= min_freq])
        self.db.executemany(
            "INSERT INTO corpus_transition(from_kind,to_kind,genre,freq) "
            "VALUES(?,?,?,?) ON CONFLICT(from_kind,to_kind,genre) DO UPDATE SET "
            "freq=freq+excluded.freq",
            [(a, b, g, f) for (a, b, g), f in self.transitions.items()])
        self.db.commit()

        out = {"vocab": len(rows), "ngrams": len(ng), "rhyme_pairs": len(rs),
               "contours": len(self.contours), "transitions": len(self.transitions)}
        self.vocab.clear()
        self.ngrams.clear()
        self.rhyme_stat.clear()
        self.contours.clear()
        self.transitions.clear()
        return out


# -- index management (bulk-load speedup) -----------------------------
_CORPUS_INDEXES = [
    ("idx_cs_genre_year", "corpus_song(genre, year)"),
    ("idx_csec_song", "corpus_section(song_id, ordinal)"),
    ("idx_csec_kind", "corpus_section(kind)"),
    ("idx_cline_song", "corpus_line(song_id, ordinal)"),
    ("idx_cline_section", "corpus_line(section_id, ordinal)"),
    ("idx_cline_rime", "corpus_line(rime_key)"),
    ("idx_cline_syl", "corpus_line(syllables)"),
    ("idx_cline_end", "corpus_line(end_word_id)"),
    ("idx_cv_freq", "corpus_vocab(freq DESC)"),
    ("idx_crs_freq", "corpus_rhyme_stat(pair_freq DESC)"),
    ("idx_ngram_freq", "corpus_ngram(n, freq DESC)"),
    ("idx_contour_freq", "corpus_contour_stat(kind, freq DESC)"),
]


def drop_corpus_indexes(db: sqlite3.Connection) -> None:
    for name, _ in _CORPUS_INDEXES:
        db.execute(f"DROP INDEX IF EXISTS {name}")
    db.commit()


def create_corpus_indexes(db: sqlite3.Connection) -> None:
    for name, spec in _CORPUS_INDEXES:
        db.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {spec}")
    db.commit()


def apply_bulk_pragmas(db: sqlite3.Connection) -> None:
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.execute("PRAGMA temp_store=MEMORY")
    db.execute("PRAGMA cache_size=-262144")   # ~256MB


def ingest_parquet(db: sqlite3.Connection, path: Path, limit: int | None = None,
                   batch: int = 40_000, store_lines: bool = True,
                   progress: int = 50_000, quiet: bool = False) -> dict:
    ph = Phonetics()
    ing = CorpusIngester(db, ph, store_lines=store_lines)
    db.execute(
        "INSERT OR REPLACE INTO shard_progress(shard,source_id,status) "
        "VALUES(?,?,'running')", (path.name, ing.source_id))
    db.commit()

    f = pq.ParquetFile(path)
    seen = kept = 0
    for rg in range(f.metadata.num_row_groups):
        t = f.read_row_group(rg, columns=["title", "artist", "tag", "year",
                                          "lyrics"])
        for title, artist, tag, year, lyrics in zip(
            t["title"].to_pylist(), t["artist"].to_pylist(),
            t["tag"].to_pylist(), t["year"].to_pylist(),
            t["lyrics"].to_pylist(),
        ):
            if limit is not None and seen >= limit:
                break
            seen += 1
            if ing.ingest_song(title, artist, tag, year, lyrics):
                kept += 1
            if len(ing._songs) >= batch:
                ing.flush_rows()
            if not quiet and seen % progress == 0:
                print(f"    {path.name}: {seen:,} seen, {kept:,} kept, "
                      f"{ing.lines_kept:,} lines", flush=True)
        if limit is not None and seen >= limit:
            break

    stats = ing.flush_aggregates()
    db.execute(
        "UPDATE shard_progress SET songs_seen=?, songs_kept=?, lines_kept=?, "
        "status='done', finished_at=datetime('now') WHERE shard=?",
        (seen, kept, ing.lines_kept, path.name))
    db.commit()
    return {"shard": path.name, "seen": seen, "kept": kept,
            "lines": ing.lines_kept, **stats}


def completed_shards(db: sqlite3.Connection) -> set[str]:
    try:
        return {r[0] for r in db.execute(
            "SELECT shard FROM shard_progress WHERE status='done'")}
    except sqlite3.OperationalError:
        return set()
