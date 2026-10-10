"""Dictionary ingestion: reference rhyme sources + craft guide into SQLite tables.

Sources (generic descriptions; see slug/filename for the input each parser reads):
  dictionary1  Rhyming slang: phrase -> word rhymes ("Rhymes on X"), register/region tags
  dictionary2  1, 2, and 3-syllable sound-keyed rhyme groups
  dictionary3  Rhymes by vowel sound + syllable tiers + unrhymables (non-rhotic)
  dictionary4  Headwords, table rhymes, (see X) cross-refs
  dictionary5  Headwords, phrases, (see X) cross-refs
  dictionary6  Unrhymable words + column-aware sound groups
  dictionary7  Syllable tiers [1]/[2]/[3] + surprising-rhyme blocks
  dictionary8  Single/double/triple sound groups
  song_starters  Craft prose: starter prompts and songwriting craft rules
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

# ---------------------------------------------------------------- shared

_WORD_OK = re.compile(r"^[a-z][a-z' \-]{0,40}$")
_SPLIT = re.compile(r"[,;]")
_WS = re.compile(r"[\s\u00a0\u2007\u202f]+")


def _deaccent(s: str) -> str:
    """Fold accents to ASCII."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


def norm_word(raw: str) -> str | None:
    """Normalise a dictionary token, or None if it is not a usable word."""
    if raw is None:
        return None
    w = _WS.sub(" ", str(raw)).strip().lower()
    w = _deaccent(w)
    w = w.strip(".,;:!?\"'()[]{}«»*`~#|_/\\-")
    w = re.sub(r"\s+", " ", w)
    if not w or len(w) > 40:
        return None
    if w.isdigit():
        return None
    if not _WORD_OK.match(w):
        return None
    return w


@dataclass
class DictStats:
    slug: str
    groups: int = 0
    members: int = 0
    pairs: int = 0
    words_new: int = 0
    tags: int = 0
    cross_refs: int = 0
    unrhymable: int = 0

    def as_row(self) -> tuple:
        return (self.groups, self.members, self.pairs, self.words_new)


class WordTable:
    """Batched word-id assignment shared across all dictionary parsers."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self.cache: dict[tuple[str, int], int] = {}
        row = db.execute("SELECT COALESCE(MAX(id),0) FROM word").fetchone()
        self.next_id = (row[0] or 0) + 1
        self.pending: list[tuple] = []
        self.new_count = 0
        for norm, wc, wid in db.execute(
                "SELECT normalized, word_count, id FROM word"):
            self.cache[(norm, wc)] = wid

    def get(self, surface: str) -> int | None:
        w = norm_word(surface)
        if w is None:
            return None
        wc = len(w.split())
        key = (w, wc)
        wid = self.cache.get(key)
        if wid is not None:
            return wid
        wid = self.next_id
        self.next_id += 1
        self.cache[key] = wid
        self.pending.append((wid, w, w, 1 if wc > 1 else 0, wc))
        self.new_count += 1
        if len(self.pending) >= 20_000:
            self.flush()
        return wid

    def flush(self) -> None:
        if self.pending:
            self.db.executemany(
                "INSERT OR IGNORE INTO word(id,surface,normalized,is_phrase,"
                "word_count) VALUES(?,?,?,?,?)", self.pending)
            self.pending.clear()


def ensure_source(db: sqlite3.Connection, slug: str, filename: str, title: str,
                  accent: str, kind: str, era: str | None = None,
                  ocr: int = 0, notes: str = "") -> int:
    db.execute(
        "INSERT OR IGNORE INTO source(slug,filename,title,accent,kind,era,"
        "ocr_damaged,notes) VALUES(?,?,?,?,?,?,?,?)",
        (slug, filename, title, accent, kind, era, ocr, notes))
    return db.execute("SELECT id FROM source WHERE slug=?", (slug,)).fetchone()[0]


class GroupWriter:
    """Writes rhyme_group + group_member rows."""

    def __init__(self, db: sqlite3.Connection, words: WordTable,
                 source_id: int, accent: str) -> None:
        self.db = db
        self.words = words
        self.source_id = source_id
        self.accent = accent
        row = db.execute("SELECT COALESCE(MAX(id),0) FROM rhyme_group").fetchone()
        self.next_gid = (row[0] or 0) + 1
        self.groups: list[tuple] = []
        self.members: list[tuple] = []
        self.stats = DictStats("")

    def add(self, word_tiers: list[tuple[str, int]], external_ref: str | None,
            vowel_key: str | None = None, surprising: int = 0,
            notes: str | None = None) -> None:
        ids: list[tuple[int, int]] = []
        seen: set[int] = set()
        for surface, tier in word_tiers:
            wid = self.words.get(surface)
            if wid is None or wid in seen:
                continue
            seen.add(wid)
            ids.append((wid, tier))
        if len(ids) < 2:
            return

        gid = self.next_gid
        self.next_gid += 1
        self.groups.append((gid, self.source_id, external_ref, vowel_key,
                            self.accent, surprising, notes))
        for ord_idx, (wid, tier) in enumerate(ids):
            self.members.append((gid, wid, tier, ord_idx))

        self.stats.groups += 1
        self.stats.members += len(ids)

        if len(self.groups) >= 5_000:
            self.flush()

    def flush(self) -> None:
        self.words.flush()
        if self.groups:
            self.db.executemany(
                "INSERT INTO rhyme_group(id,source_id,external_ref,vowel_key,"
                "accent,is_surprising,notes) VALUES(?,?,?,?,?,?,?)", self.groups)
            self.groups.clear()
        if self.members:
            self.db.executemany(
                "INSERT OR IGNORE INTO group_member(group_id,word_id,tier,"
                "ordinal) VALUES(?,?,?,?)", self.members)
            self.members.clear()


# ---------------------------------------------------------- Dictionary 1: rhyming slang

_D1_HEAD = re.compile(r"^###\s+([^►\n]+)")
_D1_RHYMES_ON = re.compile(
    r"[Rr]hymes?\s+(?:on|with)\s+['\"‘“]?([A-Za-z][A-Za-z' \-]{0,30})['\"’”]?")
_D1_SEE = re.compile(r"►\s+see\s+\[?([A-Z][A-Z' \-]{0,30})\]?(?:\([^)]*\))?")
_D1_GLOSS_LINE = re.compile(
    r"^([A-Za-z][A-Za-z' \-]{0,30})\s+\[([A-Z0-9 /:_]+)\]")
_D1_REGION = re.compile(r"\[([A-Za-z0-9 /:_]+)\]")

_REGIONS = {
    "uk", "australia", "us", "ireland", "scotland", "london", "uk: scotland",
    "south africa", "new zealand", "canada", "liverpool", "birmingham",
    "cornwall", "wales", "yorkshire", "lancashire", "durham",
}

_REGISTERS = {
    "vulgar", "derogatory", "slang", "informal", "humorous", "pejorative",
    "offensive", "euphemism", "archaic", "dialect", "obscene",
}

_DOMAINS = {
    "military", "naval", "police", "prison", "nautical", "medical",
    "theatrical", "racing", "sport", "gambling", "criminal",
}


def ingest_d1(db: sqlite3.Connection, words: WordTable,
              path: Path = ROOT / "Dictionary1.md") -> DictStats:
    """Rhyming slang: phrase -> word rhyme assertions and regional tags."""
    src = ensure_source(db, "dictionary1", "Dictionary1.md",
                        "Rhyming slang source", "en-GB-nonrhotic",
                        "slang_dict", None, 0,
                        "Phrase->word rhyme assertions and register/region tags.")
    st = DictStats("dictionary1")

    subs: list[tuple[int, int, str]] = []
    tags: list[tuple[int, str, str, int]] = []
    crossrefs: list[tuple[int, int]] = []

    head_ids: list[int] = []
    current_sense: list[str] = []

    def commit_head(hid_list: list[int], body_lines: list[str]) -> None:
        if not hid_list or not body_lines:
            return
        body = "\n".join(body_lines)
        targets: list[tuple[str, str]] = []

        for line in body_lines:
            m = _D1_GLOSS_LINE.match(line)
            if m:
                targets.append((m.group(1), "gloss"))

        for m in _D1_RHYMES_ON.finditer(body):
            targets.append((m.group(1), "rhymes_on"))

        for m in _D1_SEE.finditer(body):
            target_id = words.get(m.group(1))
            if target_id:
                for hid in hid_list:
                    if hid != target_id:
                        crossrefs.append((hid, target_id))

        for raw_target, origin in targets:
            tgt = words.get(raw_target)
            if tgt is None:
                continue
            for hid in hid_list:
                if hid != tgt:
                    subs.append((hid, tgt, origin))

        for m in _D1_REGION.finditer(body):
            for part in re.split(r"[/:]", m.group(1)):
                tag = part.strip().lower()
                if not (1 < len(tag) <= 24) or not tag.replace(" ", "").isalpha():
                    continue
                if tag in _REGIONS:
                    kind = "region"
                elif tag in _DOMAINS:
                    kind = "domain"
                else:
                    kind = "unverified"
                for hid in hid_list:
                    tags.append((hid, tag, kind, src))

        low = body.lower()
        for reg in _REGISTERS:
            if reg in low:
                for hid in hid_list:
                    tags.append((hid, reg, "register", src))

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = _WS.sub(" ", raw).strip()
        if not line:
            continue
        m = _D1_HEAD.match(line)
        if m:
            commit_head(head_ids, current_sense)
            current_sense = []
            head_ids = []
            for h in re.split(r"[;/]", m.group(1)):
                wid = words.get(h)
                if wid is not None:
                    head_ids.append(wid)
            continue
        current_sense.append(line)

    commit_head(head_ids, current_sense)
    words.flush()

    if subs:
        db.executemany(
            "INSERT OR IGNORE INTO phrase_substitution(phrase_word_id,"
            "target_word_id,sense,source_id) VALUES(?,?,?,?)",
            [(p, t, s, src) for p, t, s in subs])
    if tags:
        db.executemany(
            "INSERT OR IGNORE INTO word_tag(word_id,tag,tag_type,source_id) "
            "VALUES(?,?,?,?)", tags)
    if crossrefs:
        db.executemany(
            "INSERT OR IGNORE INTO cross_ref(from_word_id,to_word_id,"
            "source_id) VALUES(?,?,?)",
            [(s, t, src) for s, t in crossrefs])

    st.pairs = len(subs)
    st.tags = len(tags)
    st.cross_refs = len(crossrefs)
    return st


# ---------------------------------------------------------- Dictionary 2: sound-keyed groups

def ingest_d2(db: sqlite3.Connection, words: WordTable,
              path: Path = ROOT / "Dictionary2.md") -> DictStats:
    """1, 2, and 3-syllable sound-keyed rhyme groups. 3,408 bold groups organized into
    one-, two-, and three-syllable tiers.
    """
    src = ensure_source(db, "dictionary2", "Dictionary2.md",
                        "Sound-keyed rhyme source", "en-US",
                        "rhyme_dict", "1936", 0,
                        "3,408 sound-keyed rhyme groups across 1, 2, and 3 syllable tiers.")
    st = DictStats("dictionary2")
    gw = GroupWriter(db, words, src, "en-US")

    current_tier = 1
    current_header: str | None = None
    current_words: list[str] = []

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = _WS.sub(" ", raw).strip()
        if not line:
            continue

        if line.startswith("# Part Three:"):
            current_tier = 2
            continue
        elif line.startswith("# Part Four:"):
            current_tier = 3
            continue
        elif line.startswith("# Part Two:"):
            current_tier = 1
            continue
        elif line.startswith("# "):
            continue

        if line.startswith("**") and line.endswith("**"):
            if current_header and current_words:
                gw.add([(w, current_tier) for w in current_words], current_header)
            current_header = line.strip("* ")
            current_words = []
            continue

        if current_header:
            for tok in line.split():
                w = norm_word(tok)
                if w and w != current_header.lower():
                    current_words.append(w)

    if current_header and current_words:
        gw.add([(w, current_tier) for w in current_words], current_header)

    gw.flush()
    st.groups = gw.stats.groups
    st.members = gw.stats.members
    return st


# ---------------------------------------------------------- Dictionary 3: vowel-sound groups

_D3_GROUP = re.compile(r"^\*\*([0-9]+\.[0-9]+)\*\*\s*(.+)$")
_D3_VOWEL = re.compile(r"^\*\*(-[a-z]+-)\*\*")


def ingest_d3(db: sqlite3.Connection, words: WordTable,
              path: Path = ROOT / "Dictionary3.md") -> DictStats:
    """Rhymes grouped by vowel sound + unrhymable words list. British, non-rhotic."""
    src = ensure_source(db, "dictionary3", "Dictionary3.md",
                        "Vowel-sound rhyme source", "en-GB-nonrhotic",
                        "rhyme_dict", None, 0,
                        "Rhymes by vowel sound with asterisk syllable sub-tiers; unrhymables list.")
    st = DictStats("dictionary3")
    gw = GroupWriter(db, words, src, "en-GB-nonrhotic")

    unrhymables: list[str] = []
    in_unrhymable = False
    vowel: str | None = None

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = _WS.sub(" ", raw).strip()
        if not line:
            continue

        if "words that have no rhyme" in line.lower():
            in_unrhymable = True
            continue

        if in_unrhymable:
            if line.startswith("## ") or line.startswith("**"):
                in_unrhymable = False
            else:
                w = norm_word(line)
                if w:
                    unrhymables.append(w)
                continue

        m = _D3_VOWEL.match(line)
        if m and len(line) < 80:
            vowel = m.group(1)
            continue

        m = _D3_GROUP.match(line)
        if not m:
            continue

        ref, body = m.group(1), m.group(2)
        tier = 1
        members: list[tuple[str, int]] = []
        for tok in _SPLIT.split(body):
            piece = tok.strip()
            if not piece:
                continue
            if piece.startswith(("\\*", "*")):
                tier += 1
                piece = piece.lstrip("\\*").strip()
            w = norm_word(piece)
            if w:
                members.append((w, tier))
        gw.add(members, ref, vowel)

    gw.flush()

    if unrhymables:
        unrh_rows = []
        for uw in unrhymables:
            wid = words.get(uw)
            if wid:
                unrh_rows.append((wid, src))
        words.flush()
        db.executemany(
            "INSERT OR IGNORE INTO unrhymable(word_id,source_id) VALUES(?,?)", unrh_rows)
        st.unrhymable = len(unrh_rows)

    st.groups = gw.stats.groups
    st.members = gw.stats.members
    return st


# ---------------------------------------------------------- Dictionary 4: table-format rhymes

_D4_SEE = re.compile(r"\(see\s+([A-Za-z][A-Za-z' \-]{0,30})\)", re.IGNORECASE)


def ingest_d4(db: sqlite3.Connection, words: WordTable,
              path: Path = ROOT / "Dictionary4.md") -> DictStats:
    """Headwords, markdown table cells, cross-refs."""
    src = ensure_source(db, "dictionary4", "Dictionary4.md",
                        "Table-format rhyme source", "en-US",
                        "rhyme_dict", "1996", 1,
                        "Headwords and rhymes in table format, plus (see X) cross-refs.")
    st = DictStats("dictionary4")
    gw = GroupWriter(db, words, src, "en-US")

    cross: list[tuple[int, int]] = []
    cur_head: str | None = None
    cur_members: list[str] = []

    def flush_entry():
        if cur_head and cur_members:
            gw.add([(w, 1) for w in [cur_head] + cur_members], cur_head)

    lines = path.read_text(encoding="utf-8").splitlines()
    for raw in lines[840:]:
        line = _WS.sub(" ", raw).strip()
        if not line or line.startswith("| ---") or "Suffixes:" in line or "Arm 37" in line:
            continue

        cells = [c.strip() for c in line.split("|")] if "|" in line else [line]
        non_empty = [c for c in cells if c and not c.startswith("---") and not c.isdigit()]
        if not non_empty:
            continue

        first = non_empty[0].lstrip("_ ").strip()
        is_new_head = bool(re.match(r"^[A-Z][a-z\']{1,25}$", first) and not first.isupper())

        if is_new_head:
            flush_entry()
            cur_head = first
            cur_members = []
            content_cells = non_empty[1:]
        else:
            content_cells = non_empty

        if cur_head:
            head_norm = norm_word(cur_head)
            for chunk in content_cells:
                # Check for (see X)
                m_see = _D4_SEE.search(chunk)
                if m_see:
                    target_w = m_see.group(1)
                    wid_h = words.get(cur_head)
                    wid_t = words.get(target_w)
                    if wid_h and wid_t and wid_h != wid_t:
                        cross.append((wid_h, wid_t))
                    chunk = _D4_SEE.sub("", chunk)

                for tok in chunk.split():
                    w = norm_word(tok)
                    if w and w != head_norm:
                        cur_members.append(w)

    flush_entry()
    gw.flush()

    if cross:
        words.flush()
        db.executemany(
            "INSERT OR IGNORE INTO cross_ref(from_word_id,to_word_id,source_id) "
            "VALUES(?,?,?)", [(a, b, src) for a, b in cross])
        st.cross_refs = len(cross)

    st.groups = gw.stats.groups
    st.members = gw.stats.members
    return st


# ---------------------------------------------------------- Dictionary 5: headword-list rhymes

_D5_SEE = re.compile(r"^([a-z][a-z' \-]{0,30})\s*\(see ([a-z][a-z' \-]{0,30})\)\s*$")


def ingest_d5(db: sqlite3.Connection, words: WordTable,
              path: Path = ROOT / "Dictionary5.md") -> DictStats:
    """One headword per line followed by its rhymes; preserves phrases."""
    src = ensure_source(db, "dictionary5", "Dictionary5.md",
                        "Headword-list rhyme source", "en-US",
                        "rhyme_dict", "2003", 0,
                        "Headword lines with multi-word phrases and cross-references.")
    st = DictStats("dictionary5")
    gw = GroupWriter(db, words, src, "en-US")

    best: dict[str, list[str]] = {}
    cross: list[tuple[int, int]] = []

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = _WS.sub(" ", raw).strip()
        if not line or line.startswith(("|", "#", "©")):
            continue

        m = _D5_SEE.match(line)
        if m:
            a, b = words.get(m.group(1)), words.get(m.group(2))
            if a and b and a != b:
                cross.append((a, b))
            continue

        if not re.match(r"^[a-z]", line):
            continue

        toks = [t for t in line.split(" ") if t]
        if len(toks) < 2:
            continue
        head = norm_word(toks[0])
        if head is None:
            continue

        members = [head]
        # Reconstruct multi-word phrases or single words
        idx = 1
        while idx < len(toks):
            tok = toks[idx]
            # check if pair forms a phrase (e.g. boy scout, turtle dove)
            if idx + 1 < len(toks):
                pair = f"{tok} {toks[idx+1]}"
                w_pair = norm_word(pair)
                if w_pair and (" " in w_pair):
                    members.append(w_pair)
                    idx += 2
                    continue
            w = norm_word(tok)
            if w and w != head:
                members.append(w)
            idx += 1

        if len(members) > len(best.get(head, [])):
            best[head] = members

    for head, members in best.items():
        gw.add([(w, 1) for w in members], head)

    gw.flush()

    if cross:
        words.flush()
        db.executemany(
            "INSERT OR IGNORE INTO cross_ref(from_word_id,to_word_id,source_id) "
            "VALUES(?,?,?)", [(a, b, src) for a, b in cross])
        st.cross_refs = len(cross)

    st.groups = gw.stats.groups
    st.members = gw.stats.members
    return st


# ---------------------------------------------------------- Dictionary 6: 1981 rhyming dictionary

def ingest_d6(db: sqlite3.Connection, words: WordTable,
              path: Path = ROOT / "Dictionary6.md") -> DictStats:
    """A print rhyming dictionary (1981).
    Unrhymable words table + column-aware sound groups.
    """
    src = ensure_source(db, "dictionary6", "Dictionary6.md",
                        "Print rhyming dictionary (1981)", "en-US",
                        "rhyme_dict", "1981", 1,
                        "Unrhymables list plus multi-column print rhyme groups.")
    st = DictStats("dictionary6")
    gw = GroupWriter(db, words, src, "en-US")

    unrhymables: list[str] = ["orange", "silver", "purple"]
    in_unrhymable = True

    col_headers: dict[int, str] = {}
    col_words: dict[int, list[str]] = {}

    lines = path.read_text(encoding="utf-8").splitlines()
    for line_idx, raw in enumerate(lines):
        line = _WS.sub(" ", raw).strip()
        if not line or line.startswith("| ---"):
            continue

        if "MASCULINE RHYMES" in line or line_idx > 250:
            in_unrhymable = False

        if in_unrhymable:
            if "|" in line:
                for cell in line.split("|"):
                    w = norm_word(cell)
                    if w and len(w) > 1 and not w.isdigit():
                        unrhymables.append(w)
            continue

        # Rhyme section: column-aware stream parsing
        if "|" in line:
            raw_cells = line.split("|")[1:-1]
            for idx, c in enumerate(raw_cells):
                val = c.strip()
                if not val or val.isdigit() or val.startswith("---") or "(see " in val:
                    continue
                # Sound header pattern
                m_hdr = re.match(r"^[A-Z]{1,6}(\s*\([A-Z0-9\s\/\,]+\))?$", val)
                if m_hdr and len(val) <= 12:
                    if col_headers.get(idx) and col_words.get(idx):
                        gw.add([(w, 1) for w in col_words[idx]], col_headers[idx])
                    col_headers[idx] = val
                    col_words[idx] = []
                elif col_headers.get(idx):
                    w = norm_word(val)
                    if w:
                        col_words[idx].append(w)
        else:
            # Check for plain line sound header
            m_hdr = re.match(r"^[A-Z]{1,6}(\s*\([A-Z0-9\s\/\,]+\))?$", line)
            if m_hdr and len(line) <= 12:
                if col_headers.get(0) and col_words.get(0):
                    gw.add([(w, 1) for w in col_words[0]], col_headers[0])
                col_headers[0] = line
                col_words[0] = []

    for idx in col_headers:
        if col_headers.get(idx) and col_words.get(idx):
            gw.add([(w, 1) for w in col_words[idx]], col_headers[idx])

    gw.flush()

    if unrhymables:
        unrh_rows = []
        for uw in unrhymables:
            wid = words.get(uw)
            if wid:
                unrh_rows.append((wid, src))
        words.flush()
        db.executemany(
            "INSERT OR IGNORE INTO unrhymable(word_id,source_id) VALUES(?,?)", unrh_rows)
        st.unrhymable = len(unrh_rows)

    st.groups = gw.stats.groups
    st.members = gw.stats.members
    return st


# ---------------------------------------------------------- Dictionary 7: surprising-rhyme blocks

_D7_SEC = re.compile(r"^([0-9]+\.[0-9]+\.[0-9]+)\s+[\"“]--([a-z]+)[\"”]\s+\(as in [\"“]([a-z]+)[\"”]\)")
_D7_TIER = re.compile(r"^\[([1-9])\]\s+(.+)$")
_D7_SURPRISE = re.compile(r"^SURPRISING RHYMING:")
_D7_LINK = re.compile(r"\[Return to [^\]]+\]\([^)]+\)")


def ingest_d7(db: sqlite3.Connection, words: WordTable,
              path: Path = ROOT / "Dictionary7.md") -> DictStats:
    """Non-rhotic. Standard syllable tiers + cluster-by-cluster surprising-rhyme blocks."""
    src = ensure_source(db, "dictionary7", "Dictionary7.md",
                        "Surprising-rhyme source", "en-GB-nonrhotic",
                        "rhyme_dict", "2018", 0,
                        "Syllable tiers [1]/[2]/[3] and 1,391 editorial surprising rhyme blocks.")
    st = DictStats("dictionary7")
    gw = GroupWriter(db, words, src, "en-GB-nonrhotic")

    ref: str | None = None
    vowel: str | None = None
    in_surprise = False
    cur_tier = 1

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = _D7_LINK.sub(" ", raw)
        line = _WS.sub(" ", line).strip()
        if not line or line.startswith("……"):
            continue

        m_sec = _D7_SEC.match(line)
        if m_sec:
            ref = m_sec.group(1)
            vowel = m_sec.group(2)
            in_surprise = False
            cur_tier = 1
            continue

        if _D7_SURPRISE.match(line):
            in_surprise = True
            continue

        m_tier = _D7_TIER.match(line)
        if m_tier:
            cur_tier = int(m_tier.group(1))
            body = m_tier.group(2)
        else:
            body = line

        if not ref:
            continue

        # Parse comma-separated words on this line
        tokens = [norm_word(tok) for tok in _SPLIT.split(body)]
        words_on_line = [w for w in tokens if w]

        if len(words_on_line) >= 2:
            if in_surprise:
                # Group cluster-by-cluster with is_surprising=1
                gw.add([(w, cur_tier) for w in words_on_line], ref, vowel, surprising=1)
            else:
                gw.add([(w, cur_tier) for w in words_on_line], ref, vowel, surprising=0)

    gw.flush()
    st.groups = gw.stats.groups
    st.members = gw.stats.members
    return st


# ---------------------------------------------------------- Dictionary 8: sound-based groups

_D8_HEADER = re.compile(r"^[a-z]{1,6}\s*\([a-zāēīōū]+\)", re.IGNORECASE)


def ingest_d8(db: sqlite3.Connection, words: WordTable,
              path: Path = ROOT / "Dictionary8.md") -> DictStats:
    """Sound-keyed rhyming dictionary: column-aware tables of single/double/triple sounds."""
    src = ensure_source(db, "dictionary8", "Dictionary8.md",
                        "Sound-Keyed Rhyming Dictionary", "en-US",
                        "rhyme_dict", None, 0,
                        "Sound-keyed rhyme tables across single/double/triple rhymes.")
    st = DictStats("dictionary8")
    gw = GroupWriter(db, words, src, "en-US")

    col_headers: dict[int, str] = {}
    col_words: dict[int, list[str]] = {}

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = _WS.sub(" ", raw).strip()
        if not line or line.startswith("| ---") or "sounds" in line:
            continue

        if "|" in line:
            raw_cells = line.split("|")[1:-1]
            for idx, c in enumerate(raw_cells):
                val = c.strip()
                if not val or val.isdigit() or val.startswith("---"):
                    continue
                m_hdr = _D8_HEADER.match(val)
                if m_hdr:
                    if col_headers.get(idx) and col_words.get(idx):
                        gw.add([(w, 1) for w in col_words[idx]], col_headers[idx])
                    col_headers[idx] = m_hdr.group(0)
                    col_words[idx] = []
                elif col_headers.get(idx):
                    w = norm_word(re.sub(r"[\(\)\[\]]", "", val))
                    if w:
                        col_words[idx].append(w)
        else:
            m_hdr = _D8_HEADER.match(line)
            if m_hdr:
                if col_headers.get(0) and col_words.get(0):
                    gw.add([(w, 1) for w in col_words[0]], col_headers[0])
                col_headers[0] = m_hdr.group(0)
                col_words[0] = []

    for idx in col_headers:
        if col_headers.get(idx) and col_words.get(idx):
            gw.add([(w, 1) for w in col_words[idx]], col_headers[idx])

    gw.flush()
    st.groups = gw.stats.groups
    st.members = gw.stats.members
    return st


# ---------------------------------------------------------- Song starters

_PART_RE = re.compile(r"^#*\s*PART\s+([0-9IVXLCDM]+)[:\s-]*(.*)$", re.IGNORECASE)
_CUE_RE = re.compile(r"^»\s*(.+)$")
_STAGE_RE = re.compile(r"^(STAGE\s+[0-9]+)[:\s-]*(.*)$", re.IGNORECASE)


def ingest_song_starters(db: sqlite3.Connection, words: WordTable,
                         path: Path = ROOT / "Song starters.md") -> DictStats:
    """Craft prose -> starter_prompt and craft_rule rows."""
    src = ensure_source(db, "song_starters", "Song starters.md",
                        "Song Starters Craft Guide", "en-US",
                        "craft_guide", None, 0,
                        "Drafting cues and craft rules from Song starters.")
    st = DictStats("song_starters")

    current_part = "Part I"
    current_stage = "Stage 1"
    cues: list[tuple[str, str, str, int, str]] = []

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = _WS.sub(" ", raw).strip()
        if not line:
            continue

        m_part = _PART_RE.match(line)
        if m_part:
            current_part = f"Part {m_part.group(1)}: {m_part.group(2)}".strip(": ")
            continue

        m_stage = _STAGE_RE.match(line)
        if m_stage:
            current_stage = f"{m_stage.group(1)}: {m_stage.group(2)}".strip(": ")
            continue

        m_cue = _CUE_RE.match(line)
        if m_cue:
            cue_text = m_cue.group(1).strip()
            cues.append((current_part, cue_text, current_stage, src, "cue"))

    if cues:
        db.executemany(
            "INSERT OR IGNORE INTO starter_prompt(part,cue,stage,source_id,source_ref) "
            "VALUES(?,?,?,?,?)", cues)
    st.groups = len(cues)
    return st


# ---------------------------------------------------------------- driver

PARSERS = {
    "dictionary1": ingest_d1,
    "dictionary2": ingest_d2,
    "dictionary3": ingest_d3,
    "dictionary4": ingest_d4,
    "dictionary5": ingest_d5,
    "dictionary6": ingest_d6,
    "dictionary7": ingest_d7,
    "dictionary8": ingest_d8,
    "song_starters": ingest_song_starters,
}


def completed(db: sqlite3.Connection) -> set[str]:
    try:
        return {r[0] for r in db.execute(
            "SELECT slug FROM dict_progress WHERE status='done'")}
    except sqlite3.OperationalError:
        return set()


def ingest_all(db: sqlite3.Connection, only: list[str] | None = None,
               verbose: bool = True) -> dict[str, DictStats]:
    words = WordTable(db)
    done = completed(db)
    out: dict[str, DictStats] = {}

    todo = [s for s in PARSERS if (not only or s in only) and s not in done]
    for slug in todo:
        if verbose:
            print(f"  {slug}...", flush=True)
        db.execute(
            "INSERT OR REPLACE INTO dict_progress(slug,filename,status) "
            "VALUES(?,?,'running')", (slug, f"{slug}.md"))
        db.commit()
        st = PARSERS[slug](db, words)
        words.flush()
        db.execute(
            "UPDATE dict_progress SET groups=?,members=?,pairs=?,words_new=?,"
            "status='done',finished_at=datetime('now') WHERE slug=?",
            (st.groups, st.members, st.pairs, words.new_count, slug))
        db.commit()
        out[slug] = st
        if verbose:
            print(f"    -> groups={st.groups:,} members={st.members:,} "
                  f"pairs={st.pairs:,} words={words.new_count:,} "
                  f"tags={st.tags:,} unrhymable={st.unrhymable:,}", flush=True)

    populate_unified_rhymes(db, verbose=verbose)
    return out

def populate_unified_rhymes(db: sqlite3.Connection, verbose: bool = True) -> int:
    """Build the completely unified independent rhyme database from all ingested sources."""
    if verbose:
        print("  building unified rhyme table...", flush=True)

    db.execute("""
    CREATE TABLE IF NOT EXISTS unified_rhyme (
        word             TEXT NOT NULL,
        rhyme_word       TEXT NOT NULL,
        syllables        INTEGER,
        kind             TEXT NOT NULL,
        corpus_freq      INTEGER DEFAULT 0,
        pair_freq        INTEGER DEFAULT 0,
        verdict          TEXT DEFAULT 'fresh',
        source_count     INTEGER DEFAULT 1,
        PRIMARY KEY(word, rhyme_word, kind)
    );
    """)
    db.commit()

    db.execute("""
    INSERT OR IGNORE INTO unified_rhyme (word, rhyme_word, syllables, kind, source_count)
    WITH bounded_groups AS (
        SELECT group_id FROM group_member GROUP BY group_id HAVING count(*) <= 100
    )
    SELECT
        w1.normalized AS word,
        w2.normalized AS rhyme_word,
        COALESCE(p2.syllables, m2.tier, 1) AS syllables,
        CASE WHEN g.is_surprising = 1 THEN 'surprising' ELSE 'perfect' END AS kind,
        count(DISTINCT g.source_id) AS source_count
    FROM bounded_groups bg
    JOIN rhyme_group g ON g.id = bg.group_id
    JOIN group_member m1 ON m1.group_id = g.id
    JOIN group_member m2 ON m2.group_id = g.id AND m2.word_id != m1.word_id
    JOIN word w1 ON w1.id = m1.word_id
    JOIN word w2 ON w2.id = m2.word_id
    LEFT JOIN pronunciation p2 ON p2.word_id = w2.id
    GROUP BY w1.normalized, w2.normalized, kind;
    """)
    db.commit()

    db.execute("""
    INSERT OR IGNORE INTO unified_rhyme (word, rhyme_word, syllables, kind, source_count)
    SELECT
        wt.normalized AS word,
        wp.normalized AS rhyme_word,
        wp.word_count AS syllables,
        'phrase' AS kind,
        1 AS source_count
    FROM phrase_substitution ps
    JOIN word wp ON wp.id = ps.phrase_word_id
    JOIN word wt ON wt.id = ps.target_word_id
    UNION
    SELECT
        wp.normalized AS word,
        wt.normalized AS rhyme_word,
        COALESCE(pt.syllables, 1) AS syllables,
        'phrase' AS kind,
        1 AS source_count
    FROM phrase_substitution ps
    JOIN word wp ON wp.id = ps.phrase_word_id
    JOIN word wt ON wt.id = ps.target_word_id
    LEFT JOIN pronunciation pt ON pt.word_id = wt.id;
    """)
    db.commit()

    db.execute("CREATE INDEX IF NOT EXISTS idx_ur_word ON unified_rhyme(word);")
    db.execute("CREATE INDEX IF NOT EXISTS idx_ur_word_syl ON unified_rhyme(word, syllables);")
    db.execute("CREATE INDEX IF NOT EXISTS idx_ur_word_kind ON unified_rhyme(word, kind);")
    db.commit()

    total = db.execute("SELECT count(*) FROM unified_rhyme").fetchone()[0]
    if verbose:
        print(f"    -> unified_rhyme: {total:,} rows indexed and ready", flush=True)
    return total
