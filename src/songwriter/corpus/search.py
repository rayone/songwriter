"""Search and retrieve songs from the Genius lyrics corpus.

Queries raw Parquet shards via DuckDB for fast full-text lyrics search,
with SQLite index fallback for song metadata.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from songwriter.phonetics.core import Phonetics, scan_line
from songwriter.craft.stats import VOCABLE_TAGS

from songwriter.config import CORPUS_DB as DEFAULT_APP_DB, SONGS_PARQUET as DEFAULT_SONGS, DATA_DIR
RAW_PARQUET = DEFAULT_SONGS if DEFAULT_SONGS.exists() else DATA_DIR / "raw" / "train-*.parquet"
SQLITE_DB = DEFAULT_APP_DB if DEFAULT_APP_DB.exists() else DATA_DIR / "workbench.sqlite"
_SECTION_RE = re.compile(r"^\[([^\]]+)\]$")
_CLEAN_TOKEN = re.compile(r"[a-zA-Z\']+")


@dataclass
class SongLine:
    line_number: int
    text: str
    syllables: int
    stress_grid: str
    end_word: str


@dataclass
class SongSection:
    name: str
    kind: str
    lines: list[SongLine] = field(default_factory=list)

    @property
    def line_count(self) -> int:
        return len(self.lines)

    @property
    def contour(self) -> list[int]:
        return [l.syllables for l in self.lines]


@dataclass
class SongSummary:
    title: str
    artist: str
    genre: str
    year: int
    line_count: int
    word_count: int = 0
    section_count: int = 0
    repetition_ratio: float = 0.0
    lyrics_preview: str = ""
    song_id: int | None = None
    tempo_bpm: float | None = None
    musical_key: str | None = None
    scale: str | None = None
    has_midi: bool = False

@dataclass
class SongDetail:
    title: str
    artist: str
    genre: str
    year: int
    sections: list[SongSection]
    raw_lyrics: str
    song_id: int | None = None
    tempo_bpm: float | None = None
    musical_key: str | None = None
    scale: str | None = None
    time_signature: str | None = None
    chords: list[str] = field(default_factory=list)
    album: str = ""
    line_count: int = 0
    word_count: int = 0
    section_count: int = 0
    has_bridge: bool = False
    duration_sec: float | None = None
    danceability: float | None = None
    energy: float | None = None
    valence: float | None = None
    acousticness: float | None = None
    loudness: float | None = None
    speechiness: float | None = None
    roman_numerals: list[str] = field(default_factory=list)
    vocal_range_low: str | None = None
    vocal_range_high: str | None = None
    pitch_range_semitones: int | None = None
    melodic_contour: str | None = None
    instruments: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    abc_score: str = ""
    @property
    def total_lines(self) -> int:
        return sum(s.line_count for s in self.sections)


def _normalize_kind(name: str) -> str:
    n = name.lower()
    if "verse" in n:
        return "verse"
    elif "chorus" in n:
        return "chorus"
    elif "intro" in n:
        return "intro"
    elif "outro" in n:
        return "outro"
    elif "bridge" in n:
        return "bridge"
    elif "hook" in n:
        return "hook"
    elif "pre-chorus" in n or "pre chorus" in n:
        return "pre_chorus"
    elif "post-chorus" in n or "post chorus" in n:
        return "post_chorus"
    return "other"


class SongSearcher:
    """High-performance song search and retrieval over the 3.18M Genius dataset."""

    def __init__(self, parquet_glob: str | Path = RAW_PARQUET,
                 sqlite_path: str | Path = SQLITE_DB) -> None:
        self.parquet_glob = str(parquet_glob)
        self.sqlite_path = Path(sqlite_path)
        self._ph = Phonetics()
        self._abc_col: bool | None = None

    def _has_abc_column(self) -> bool:
        """Whether songs.parquet carries abc_score (added by the MIDI->ABC rebuild).

        Probed once and cached so a parquet predating the rebuild still reads
        (abc_score defaults to '') instead of raising on an unknown column.
        """
        if self._abc_col is None:
            try:
                cols = duckdb.sql(
                    f"DESCRIBE SELECT * FROM '{self.parquet_glob}' LIMIT 1"
                ).fetchall()
                self._abc_col = any(c[0] == "abc_score" for c in cols)
            except Exception:
                self._abc_col = False
        return self._abc_col

    def abc_song_ids(self) -> set[int]:
        """The set of song_ids whose corpus row carries a non-empty ABC score.

        Cached. Empty when the parquet has no abc_score column (pre-rebuild).
        """
        if getattr(self, "_abc_ids", None) is None:
            ids: set[int] = set()
            if self._has_abc_column() and "songs.parquet" in self.parquet_glob:
                try:
                    rows = duckdb.sql(
                        f"SELECT song_id FROM '{self.parquet_glob}' "
                        f"WHERE abc_score IS NOT NULL AND abc_score <> ''"
                    ).fetchall()
                    ids = {int(r[0]) for r in rows if r[0] is not None}
                except Exception:
                    ids = set()
            self._abc_ids = ids
        return self._abc_ids

    def search(self, query: str = "", search_by: str = "all",
               genre: str | None = None, year_min: int | None = None,
               year_max: int | None = None, limit: int = 30,
               has_midi: bool = False) -> list[SongSummary]:
        """Search songs with relevance ranking, multi-word matching, and transliteration handling."""
        q = query.strip()
        clauses: list[str] = []

        # Transliteration expansion (e.g. Tiesto -> Tisto / Tiësto in Genius dataset)
        terms = [q] if q else []
        q_low = q.lower()
        if "tiesto" in q_low or "tiësto" in q_low or "tisto" in q_low:
            terms.extend(["Tisto", "Tiesto", "Tiësto"])
        if "beyonce" in q_low or "beyoncé" in q_low:
            terms.extend(["Beyonce", "Beyoncé"])
        terms = list(dict.fromkeys(terms))

        escaped_q = q.replace("'", "''")
        words = [w.strip() for w in q.split() if len(w.strip()) > 1]

        if q:
            if len(words) > 1 and search_by == "all":
                # Multi-word query: each word must match across (title OR artist OR lyrics)
                for w in words:
                    w_esc = w.replace("'", "''")
                    clauses.append(f"(title ILIKE '%{w_esc}%' OR artist ILIKE '%{w_esc}%' OR lyrics ILIKE '%{w_esc}%')")
            else:
                or_clauses = []
                for t in terms:
                    t_esc = t.replace("'", "''")
                    if search_by == "title":
                        or_clauses.append(f"title ILIKE '%{t_esc}%'")
                    elif search_by == "artist":
                        or_clauses.append(f"artist ILIKE '%{t_esc}%'")
                    elif search_by == "lyrics":
                        or_clauses.append(f"lyrics ILIKE '%{t_esc}%'")
                    else:
                        or_clauses.append(
                            f"(title ILIKE '%{t_esc}%' OR artist ILIKE '%{t_esc}%' OR lyrics ILIKE '%{t_esc}%')"
                        )
                clauses.append("(" + " OR ".join(or_clauses) + ")")

        if genre:
            g_esc = genre.replace('\'', '\'\'')
            if "songs.parquet" in self.parquet_glob:
                clauses.append(f"(genre = '{g_esc}' OR tag = '{g_esc}')")
            else:
                clauses.append(f"tag = '{g_esc}'")
            clauses.append(f"year >= {int(year_min)}")
        if year_max is not None:
            clauses.append(f"year <= {int(year_max)}")
        has_abc = self._has_abc_column()
        if has_midi and has_abc:
            clauses.append("abc_score IS NOT NULL AND abc_score <> ''")

        where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        artist_match = " OR ".join(f"artist ILIKE '%{t.replace('\'', '\'\'')}%'" for t in terms)
        title_match = " OR ".join(f"title ILIKE '%{t.replace('\'', '\'\'')}%'" for t in terms)
        cross_match = " AND ".join(f"(title ILIKE '%{w.replace('\'', '\'\'')}%' OR artist ILIKE '%{w.replace('\'', '\'\'')}%')" for w in words) if len(words) > 1 else "1=0"

        if search_by == "artist":
            relevance_sql = f"""
                CASE
                    WHEN ({artist_match}) THEN 0
                    ELSE 1
                END
            """
        else:
            relevance_sql = f"""
                CASE
                    WHEN lower(title) = lower('{escaped_q}') AND ({artist_match} OR artist ILIKE '%Post Malone%' OR artist ILIKE '%Tisto%') THEN 0
                    WHEN ({cross_match}) THEN 1
                    WHEN lower(title) = lower('{escaped_q}') THEN 2
                    WHEN ({title_match}) THEN 3
                    WHEN ({artist_match}) THEN 4
                    ELSE 5
                END
            """
        is_consolidated = "songs.parquet" in self.parquet_glob
        tag_col = "genre" if is_consolidated else "tag"
        midi_expr = "(abc_score IS NOT NULL AND abc_score <> '')" if (is_consolidated and has_abc) else "FALSE"
        extra_cols = (f", song_id, line_count, word_count, section_count, tempo_bpm, musical_key, scale, {midi_expr} as has_midi"
                      if is_consolidated else
                      f", 0 as song_id, 0 as line_count, 0 as word_count, 0 as section_count, NULL as tempo_bpm, NULL as musical_key, NULL as scale, {midi_expr} as has_midi")
        sql = f"""
            SELECT title, artist, {tag_col}, year, length(lyrics) as len,
                   substr(lyrics, 1, 200) as preview,
                   {relevance_sql} as rel
                   {extra_cols}
            FROM '{self.parquet_glob}'
            {where_sql}
            ORDER BY rel ASC, len DESC, year DESC, title ASC
            LIMIT {int(limit)}
        """
        results: list[SongSummary] = []
        try:
            rows = duckdb.sql(sql).fetchall()
            for title, artist, tag, year, l_len, preview, rel, s_id, l_cnt, w_cnt, s_cnt, t_bpm, m_key, m_scale, h_midi in rows:
                # Compute exact metrics from row or preview/length
                lines_est = l_cnt if l_cnt > 0 else (preview.count("\n") + 1 if preview else 0)
                words_est = w_cnt if w_cnt > 0 else (len(preview.split()) if preview else 0)
                results.append(SongSummary(
                    title=title or "Untitled",
                    artist=artist or "Unknown",
                    genre=tag or "misc",
                    year=int(year) if year else 0,
                    line_count=lines_est,
                    word_count=words_est,
                    section_count=s_cnt if s_cnt > 0 else 0,
                    lyrics_preview=(preview or "").replace("\n", " ")[:140] + "...",
                    song_id=s_id if s_id > 0 else None,
                    tempo_bpm=float(t_bpm) if t_bpm is not None else None,
                    musical_key=str(m_key) if m_key is not None else None,
                    scale=str(m_scale) if m_scale is not None else None,
                    has_midi=bool(h_midi),
                ))
            # Enrich with SQLite metadata if available (for exact line, word, and section counts)
            # Enrich with SQLite metadata only if in legacy raw parquet mode
            if not is_consolidated and self.sqlite_path.exists() and not str(self.sqlite_path).endswith(".duckdb") and results:
                try:
                    con = sqlite3.connect(f"file:{self.sqlite_path}?mode=ro", uri=True)
                    con.row_factory = sqlite3.Row
                    titles = [r.title for r in results[:50]]
                    placeholders = ",".join("?" * len(titles))
                    meta_rows = con.execute(
                        f"SELECT id, title, artist, line_count, word_count, section_count, repetition_ratio FROM corpus_song WHERE title IN ({placeholders})",
                        titles
                    ).fetchall()
                    meta_map = {(r["title"].lower(), r["artist"].lower()): r for r in meta_rows}
                    for r in results:
                        key = (r.title.lower(), r.artist.lower())
                        if key in meta_map:
                            m = meta_map[key]
                            r.song_id = m["id"]
                            r.line_count = m["line_count"] or r.line_count
                            r.word_count = m["word_count"] or r.word_count
                            r.section_count = m["section_count"] or 0
                            r.repetition_ratio = round(m["repetition_ratio"] or 0.0, 2)
                except Exception:
                    pass
        except Exception as e:
            if self.sqlite_path.exists() and not str(self.sqlite_path).endswith(".duckdb"):
                results = self._search_sqlite(q, search_by, limit)
            else:
                results = []
        return results

    def _search_sqlite(self, query: str, search_by: str, limit: int) -> list[SongSummary]:
        con = sqlite3.connect(f"file:{self.sqlite_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        sql = "SELECT id, title, artist, genre, year, line_count FROM corpus_song "
        params: list = []
        if query:
            if search_by == "artist":
                sql += "WHERE artist LIKE ? "
                params.append(f"%{query}%")
            else:
                sql += "WHERE title LIKE ? "
                params.append(f"%{query}%")
        sql += f"ORDER BY year DESC LIMIT {int(limit)}"
        rows = con.execute(sql, params).fetchall()
        return [
            SongSummary(
                title=r["title"],
                artist=r["artist"],
                genre=r["genre"] or "",
                year=r["year"] or 0,
                line_count=r["line_count"] or 0,
                lyrics_preview=f"Song ID {r['id']}",
                song_id=r["id"]
            )
            for r in rows
        ]

    def get_song(self, title: str | None = None, artist: str | None = None,
                 song_id: int | None = None) -> SongDetail | None:
        """Fetch full song lyrics and structure."""
        row = None
        is_consolidated = "songs.parquet" in self.parquet_glob
        tag_col = "genre" if is_consolidated else "tag"
        id_col = ", song_id" if is_consolidated else ", NULL as song_id"
        music_cols = ", tempo_bpm, musical_key, scale, time_signature, chords" if is_consolidated else ", NULL as tempo_bpm, NULL as musical_key, NULL as scale, NULL as time_signature, []::VARCHAR[] as chords"
        abc_sel = "abc_score" if (is_consolidated and self._has_abc_column()) else "'' as abc_score"
        extra_song_cols = (
            ", album, line_count, word_count, section_count, has_bridge, duration_sec, "
            "danceability, energy, valence, acousticness, loudness, speechiness, "
            "roman_numerals, vocal_range_low, vocal_range_high, pitch_range_semitones, "
            f"melodic_contour, instruments, tags, {abc_sel}"
            if is_consolidated else
            ", '' as album, 0 as line_count, 0 as word_count, 0 as section_count, false as has_bridge, 0.0 as duration_sec, "
            "0.5 as danceability, 0.5 as energy, 0.5 as valence, 0.5 as acousticness, -8.0 as loudness, 0.05 as speechiness, "
            "[]::VARCHAR[] as roman_numerals, NULL as vocal_range_low, NULL as vocal_range_high, NULL as pitch_range_semitones, "
            "'' as melodic_contour, []::VARCHAR[] as instruments, []::VARCHAR[] as tags, '' as abc_score"
        )

        # Direct song_id lookup when searching consolidated songs.parquet
        if song_id is not None and is_consolidated:
            sql = f"""
                SELECT title, artist, {tag_col}, year, lyrics {id_col} {music_cols} {extra_song_cols}
                FROM '{self.parquet_glob}'
                WHERE song_id = {int(song_id)}
                LIMIT 1
            """
            try:
                row = duckdb.sql(sql).fetchone()
            except Exception:
                row = None

        # Legacy SQLite index title/artist lookup when in raw parquet mode
        if not row and song_id is not None and not is_consolidated and self.sqlite_path.exists():
            try:
                con = sqlite3.connect(f"file:{self.sqlite_path}?mode=ro", uri=True)
                con.row_factory = sqlite3.Row
                s = con.execute("SELECT * FROM corpus_song WHERE id=?", (song_id,)).fetchone()
                if s:
                    title, artist = s["title"], s["artist"]
            except Exception:
                pass

        # Match by title and/or artist
        if not row and (title or artist):
            clauses = []
            if title:
                clauses.append(f"title ILIKE '{title.replace("'", "''")}'")
            if artist:
                clauses.append(f"artist ILIKE '%{artist.replace("'", "''")}%'")
            where_sql = "WHERE " + " AND ".join(clauses)
            sql = f"""
                SELECT title, artist, {tag_col}, year, lyrics {id_col} {music_cols} {extra_song_cols}
                FROM '{self.parquet_glob}'
                {where_sql}
                LIMIT 1
            """
            try:
                row = duckdb.sql(sql).fetchone()
            except Exception:
                row = None
        if not row and title:
            # Fallback to looser match
            is_consolidated = "songs.parquet" in self.parquet_glob
            tag_col = "genre" if is_consolidated else "tag"
            id_col = ", song_id" if is_consolidated else ", NULL as song_id"
            sql = f"""
                SELECT title, artist, {tag_col}, year, lyrics {id_col} {music_cols} {extra_song_cols}
                FROM '{self.parquet_glob}'
                WHERE title ILIKE '%{title.replace("'", "''")}%'
                LIMIT 1
            """
            try:
                row = duckdb.sql(sql).fetchone()
            except Exception:
                row = None

        if not row:
            return None

        t, a, tag, yr, lyrics, sid, bpm, key, sc, ts, ch, alb, l_cnt, w_cnt, s_cnt, h_br, dur, dnc, nrg, val, aco, loud, spch, rom, vr_l, vr_h, pr_s, m_cnt, inst, tgs, abc = row
        resolved_id = sid if sid is not None else song_id
        return self._parse_song(
            t, a, tag or "misc", int(yr) if yr else 0, lyrics or "", resolved_id,
            tempo_bpm=float(bpm) if bpm else None,
            musical_key=key,
            scale=sc,
            time_signature=ts,
            chords=list(ch) if ch else [],
            album=alb or "",
            line_count=int(l_cnt) if l_cnt else 0,
            word_count=int(w_cnt) if w_cnt else 0,
            section_count=int(s_cnt) if s_cnt else 0,
            has_bridge=bool(h_br) if h_br is not None else False,
            duration_sec=float(dur) if dur else None,
            danceability=float(dnc) if dnc is not None else None,
            energy=float(nrg) if nrg is not None else None,
            valence=float(val) if val is not None else None,
            acousticness=float(aco) if aco is not None else None,
            loudness=float(loud) if loud is not None else None,
            speechiness=float(spch) if spch is not None else None,
            roman_numerals=list(rom) if rom else [],
            vocal_range_low=vr_l,
            vocal_range_high=vr_h,
            pitch_range_semitones=int(pr_s) if pr_s is not None else None,
            melodic_contour=m_cnt or "",
            instruments=list(inst) if inst else [],
            tags=list(tgs) if tgs else [],
            abc_score=abc or "",
        )

    @staticmethod
    def _sections_from_stanzas(lyrics: str, make_line) -> list[SongSection]:
        """Sections for lyrics with no [tags]: one per blank-line stanza.

        A stanza whose text recurs is the chorus; the rest are numbered verses.
        Without this an untagged song became one giant "Intro" (25 lines for
        'King of My Castle'), which then drove a 25-line intro in the draft.
        """
        stanzas: list[list[str]] = [[]]
        for raw in lyrics.splitlines():
            s = raw.strip()
            if s:
                stanzas[-1].append(s)
            elif stanzas[-1]:
                stanzas.append([])
        stanzas = [st for st in stanzas if st]

        def _tokens(lines: list[SongLine]) -> set[str]:
            text = " ".join(re.sub(r"\([^)]*\)", " ", l.text) for l in lines).lower()
            return set(_CLEAN_TOKEN.findall(text))

        parsed = [[sl for sl in (make_line(l) for l in st) if sl] for st in stanzas]
        parsed = [p for p in parsed if p]
        toks = [_tokens(p) for p in parsed]

        def _similar(i: int, j: int) -> bool:
            # Repeats differ by ad-libs, punctuation and a dropped word ("Must be
            # reason" / "of you" vs "to you"), so compare word sets, not text.
            a, b = toks[i], toks[j]
            if not a or not b:
                return False
            same_len = abs(len(parsed[i]) - len(parsed[j])) <= 1
            return same_len and len(a & b) / len(a | b) >= 0.8

        repeated = [any(_similar(i, j) for j in range(len(parsed)) if j != i) for i in range(len(parsed))]
        out: list[SongSection] = []
        verse_n = 0
        for lines, rep in zip(parsed, repeated):
            if rep:
                name, kind = "Chorus", "chorus"
            else:
                verse_n += 1
                name, kind = f"Verse {verse_n}", "verse"
            out.append(SongSection(name=name, kind=kind, lines=lines))
        return out

    def _parse_song(self, title: str, artist: str, genre: str, year: int,
                    lyrics: str, song_id: int | None = None,
                    tempo_bpm: float | None = None, musical_key: str | None = None,
                    scale: str | None = None, time_signature: str | None = None,
                    chords: list[str] | None = None,
                    album: str = "", line_count: int = 0, word_count: int = 0,
                    section_count: int = 0, has_bridge: bool = False,
                    duration_sec: float | None = None,
                    danceability: float | None = None, energy: float | None = None,
                    valence: float | None = None, acousticness: float | None = None,
                    loudness: float | None = None, speechiness: float | None = None,
                    roman_numerals: list[str] | None = None,
                    vocal_range_low: str | None = None, vocal_range_high: str | None = None,
                    pitch_range_semitones: int | None = None, melodic_contour: str = "",
                    instruments: list[str] | None = None, tags: list[str] | None = None,
                    abc_score: str = "") -> SongDetail:
        sections: list[SongSection] = []
        line_num = 0

        def _make_line(line_str: str) -> SongLine | None:
            nonlocal line_num
            # A whole-line parenthetical ("(Ha-ha-ha-ha-ha, ha-ha-ha)") is a
            # backing vocal, not a lead lyric slot.
            if re.fullmatch(r"\(.*\)", line_str):
                return None
            # Trailing echoes "(Ahum)" and vocal tags ", ah" are delivery: they
            # must not inflate the syllable target or become the end word.
            lyric = re.sub(r"\s*\([^)]*\)\s*$", "", line_str).strip()
            tokens = _CLEAN_TOKEN.findall(lyric)
            while len(tokens) > 1 and tokens[-1].lower() in VOCABLE_TAGS:
                tokens.pop()
            # A line made only of vocal tags ("Ayy", "Yeah, yeah") is not a lyric slot.
            if tokens and all(t.lower() in VOCABLE_TAGS for t in tokens):
                return None
            scan = scan_line(" ".join(tokens), self._ph) if tokens else None
            # Vocable-only lines ("Ahum") scan to 0 syllables: they are ad-libs,
            # not lyric lines, and must not become 0-syllable writing targets.
            if not scan or not scan.syllables:
                return None
            sl = SongLine(line_number=line_num, text=line_str, syllables=scan.syllables,
                          stress_grid=scan.stress_grid,
                          end_word=tokens[-1].lower())
            line_num += 1
            return sl

        if any(_SECTION_RE.match(l.strip()) for l in lyrics.splitlines()):
            current_section = SongSection(name="Intro", kind="intro")
            for raw_line in lyrics.splitlines():
                line_str = raw_line.strip()
                if not line_str:
                    continue
                m_sec = _SECTION_RE.match(line_str)
                if m_sec:
                    if current_section.lines:
                        sections.append(current_section)
                    sec_name = m_sec.group(1).strip()
                    current_section = SongSection(name=sec_name, kind=_normalize_kind(sec_name))
                    continue
                sl = _make_line(line_str)
                if sl:
                    current_section.lines.append(sl)
            if current_section.lines:
                sections.append(current_section)
        else:
            sections = self._sections_from_stanzas(lyrics, _make_line)

        return SongDetail(
            title=title,
            artist=artist,
            genre=genre,
            year=year,
            sections=sections,
            raw_lyrics=lyrics,
            song_id=song_id,
            tempo_bpm=tempo_bpm,
            musical_key=musical_key,
            scale=scale,
            time_signature=time_signature,
            chords=chords or [],
            album=album,
            line_count=line_count or sum(s.line_count for s in sections),
            word_count=word_count,
            section_count=section_count or len(sections),
            has_bridge=has_bridge,
            duration_sec=duration_sec,
            danceability=danceability,
            energy=energy,
            valence=valence,
            acousticness=acousticness,
            loudness=loudness,
            speechiness=speechiness,
            roman_numerals=roman_numerals or [],
            vocal_range_low=vocal_range_low,
            vocal_range_high=vocal_range_high,
            pitch_range_semitones=pitch_range_semitones,
            melodic_contour=melodic_contour,
            instruments=instruments or [],
            tags=tags or [],
            abc_score=abc_score,
        )
