"""MusicStudio Production Templates local engine.

Queries the 5,960 production templates cloned locally into data/workbench.sqlite
(tables: studio_prompts, studio_prompt_segments, studio_prompts_fts).
Provides semantic full-text search, genre/mood filtering, and native
prompt formatting for YuE2 and MiniMax Music 03.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

import duckdb

from songwriter.db import Database

from songwriter.config import CORPUS_DB as DEFAULT_DB


class DuckRow(dict):
    def __getitem__(self, item: Any) -> Any:
        if isinstance(item, int):
            return list(self.values())[item]
        return super().__getitem__(item)


class DuckConnectionWrapper:
    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self._con = con

    def execute(self, sql: str, params: Any = None) -> Any:
        cur = self._con.execute(sql, params) if params is not None else self._con.execute(sql)
        return DuckCursorWrapper(cur)

    def close(self) -> None:
        """No-op: the cursor belongs to the process-wide gateway."""


class DuckCursorWrapper:
    def __init__(self, cur: Any) -> None:
        self._cur = cur

    def fetchall(self) -> list[DuckRow]:
        rows = self._cur.fetchall()
        if not rows:
            return []
        cols = [c[0] for c in self._cur.description]
        return [DuckRow(zip(cols, r)) for r in rows]

    def fetchone(self) -> DuckRow | None:
        r = self._cur.fetchone()
        if r is None:
            return None
        cols = [c[0] for c in self._cur.description]
        return DuckRow(zip(cols, r))


class MusicStudioDB:
    def __init__(self, db_path: str | Path = DEFAULT_DB) -> None:
        self.db_path = Path(db_path)

    @property
    def _is_duckdb(self) -> bool:
        return str(self.db_path).endswith(".duckdb")

    @property
    def _table(self) -> str:
        return "studio_templates" if self._is_duckdb else "studio_prompts"

    @property
    def is_available(self) -> bool:
        return self.db_path.exists()

    def _con(self) -> Any | None:
        if not self.is_available:
            return None
        if self._is_duckdb:
            return DuckConnectionWrapper(Database.get(self.db_path, read_only=True).conn)
        c = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        return c
    def list_genres(self, limit: int = 50) -> list[dict[str, Any]]:
        con = self._con()
        if not con:
            return []
        rows = con.execute(f"""
            SELECT genre, count(*) as count
            FROM {self._table}
            WHERE genre != ''
            GROUP BY genre
            ORDER BY count DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [{"genre": r["genre"], "count": r["count"]} for r in rows]

    def list_subgenres(self, genre: str, limit: int = 30) -> list[str]:
        con = self._con()
        if not con:
            return []
        rows = con.execute(f"""
            SELECT DISTINCT subgenre
            FROM {self._table}
            WHERE genre = ? AND subgenre != ''
            ORDER BY subgenre ASC
            LIMIT ?
        """, (genre, limit)).fetchall()
        return [r["subgenre"] for r in rows]

    def style_vocabulary(self) -> dict[str, list[str]]:
        """Every style term the templates actually carry, longest-first.

        The genre/subgenre/vocal columns are the authority on what can be matched:
        a hardcoded list drifts from the data and silently fails to match. Ordered
        longest-first so 'Deep House' wins over 'House' during substring scanning.
        """
        con = self._con()
        if not con:
            return {"genre": [], "subgenre": [], "vocal": []}
        try:
            out: dict[str, list[str]] = {}
            for col in ("genre", "subgenre", "vocal"):
                rows = con.execute(f"""
                    SELECT DISTINCT {col} AS term
                    FROM {self._table}
                    WHERE {col} IS NOT NULL AND trim({col}) != ''
                """).fetchall()
                terms = {str(r["term"]).strip() for r in rows}
                terms.discard("unknown")
                out[col] = sorted(terms, key=lambda t: (-len(t), t.lower()))
            return out
        finally:
            con.close()

    def search_templates(self, query: str = "", genre: str | None = None,
                         limit: int = 500) -> list[dict[str, Any]]:
        """Structured search across curated production templates."""
        con = self._con()
        if not con:
            return []

        q = query.strip()
        clauses = []
        params: list[Any] = []
        token_list = []

        clean_q = re.sub(r"[^\w\s-]", " ", q).strip() if q else ""
        tokens = [t for t in re.split(r"[\s/]+", clean_q) if len(t) > 1]
        token_list = tokens

        if q:
            exact_sub = f"%{q}%"
            token_or = ["(p.title ILIKE ? OR p.genre ILIKE ? OR p.subgenre ILIKE ? OR p.audio_prompt ILIKE ?)"]
            params.extend([exact_sub, exact_sub, exact_sub, exact_sub])
            for t in tokens:
                wild = f"%{t}%"
                token_or.append("(p.title ILIKE ? OR p.genre ILIKE ? OR p.subgenre ILIKE ? OR p.audio_prompt ILIKE ?)")
                params.extend([wild, wild, wild, wild])
            clauses.append("(" + " OR ".join(token_or) + ")")

        if genre and genre.strip():
            # Genres come from the table itself (/api/templates/genres), so match exactly:
            # a substring match made "Pop" pull in every "*pop*" title and subgenre.
            clauses.append("p.genre = ?")
            params.append(genre.strip())

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        relevance_clauses = []
        if q:
            q_esc = q.replace("'", "''")
            relevance_clauses.append(f"CASE WHEN p.title ILIKE '%{q_esc}%' THEN 0 ELSE 1 END")
            relevance_clauses.append(f"CASE WHEN p.genre ILIKE '%{q_esc}%' OR p.subgenre ILIKE '%{q_esc}%' THEN 0 ELSE 1 END")
        for t in token_list:
            t_esc = t.replace("'", "''")
            relevance_clauses.append(f"CASE WHEN p.title ILIKE '%{t_esc}%' THEN 0 ELSE 1 END")
            relevance_clauses.append(f"CASE WHEN p.genre ILIKE '%{t_esc}%' OR p.subgenre ILIKE '%{t_esc}%' THEN 0 ELSE 1 END")
        rel_order = (", ".join(relevance_clauses) + ", ") if relevance_clauses else ""

        sql = f"""
            SELECT p.id, p.slug, p.title, p.genre, p.subgenre, p.bpm, p.music_key, p.scale, p.vocal, p.audio_prompt, p.pro_tip, '' as match_snippet
            FROM {self._table} p
            {where}
            ORDER BY {rel_order}p.is_favorite DESC, p.id ASC
            LIMIT ?
        """
        params.append(limit)
        rows = con.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    def get_template_detail(self, prompt_id: int) -> dict[str, Any] | None:
        con = self._con()
        if not con:
            return None

        if self._is_duckdb:
            row = con.execute("SELECT * FROM studio_templates WHERE id = ?", (prompt_id,)).fetchone()
            if not row:
                return None
            p_dict = dict(row)
            segments_list = p_dict.pop("segments", []) or []
            p_dict.pop("keywords", None)
            segs: dict[str, dict[str, str]] = {}
            for s in segments_list:
                segs.setdefault(s["section"], {})[s["field"]] = s["content"]
        else:
            row = con.execute("SELECT * FROM studio_prompts WHERE id = ?", (prompt_id,)).fetchone()
            if not row:
                return None
            segments = con.execute(
                "SELECT section, field, content FROM studio_prompt_segments WHERE prompt_id = ? ORDER BY ordinal",
                (prompt_id,)
            ).fetchall()
            segs = {}
            for s in segments:
                segs.setdefault(s["section"], {})[s["field"]] = s["content"]
            p_dict = dict(row)
        if "title" in p_dict and p_dict["title"]:
            p_dict["title"] = re.sub(r"\s*#\d+$", "", str(p_dict["title"]))

        yue2_style = self.format_for_yue2(p_dict, segs)
        minimax_caption = self.format_for_minimax(p_dict, segs)
        songbench_style = self.format_for_songbench(p_dict, segs)

        return {
            "metadata": p_dict,
            "segments": segs,
            "yue2_style": yue2_style,
            "minimax_caption": minimax_caption,
            "songbench_style": songbench_style,
        }

    def get_best_match(self, genre: str = "Hip-Hop", subgenre: str | None = None) -> dict[str, Any] | None:
        con = self._con()
        if not con:
            return None

        clean_genre = genre.strip()
        sql = f"SELECT id FROM {self._table} WHERE genre ILIKE ? "
        params = [f"%{clean_genre}%"]
        if subgenre:
            sql += "AND subgenre ILIKE ? "
            params.append(f"%{subgenre}%")
        sql += "ORDER BY is_favorite DESC, id ASC LIMIT 1"

        row = con.execute(sql, params).fetchone()
        if not row:
            # Fallback to search query
            candidates = self.search_templates(query=genre, limit=1)
            if candidates:
                return self.get_template_detail(candidates[0]["id"])
            row = con.execute(f"SELECT id FROM {self._table} ORDER BY is_favorite DESC LIMIT 1").fetchone()

        if row:
            return self.get_template_detail(row["id"])
        return None

    @staticmethod
    def format_for_yue2(prompt_row: dict[str, Any], segs: dict[str, dict[str, str]]) -> str:
        bpm = prompt_row.get("bpm") or 90
        genre_str = f"{prompt_row.get('genre', '')} / {prompt_row.get('subgenre', '')}".strip(" /")
        vocal = segs.get("vocal_details", {}).get("vocal_gender_timbre", prompt_row.get("vocal", "melodic vocal")).split(".")[0].strip()
        primary = segs.get("arrangement", {}).get("primary_layer", "").split(".")[0].strip()
        groove = segs.get("arrangement", {}).get("groove_foundation", "").split(".")[0].strip()

        parts = ["English", f"{bpm} BPM"]
        if genre_str:
            parts.append(genre_str)
        if vocal and "no lead vocal" not in vocal.lower() and vocal.lower() != "instrumental":
            parts.append(f"{vocal} vocals")
        elif vocal:
            parts.append(vocal)
        if primary:
            parts.append(primary)
        if groove:
            parts.append(groove)
        return ", ".join(parts)

    @staticmethod
    def format_for_minimax(prompt_row: dict[str, Any], segs: dict[str, dict[str, str]]) -> str:
        bpm = prompt_row.get("bpm") or 90
        key = prompt_row.get("music_key") or "C"
        scale = prompt_row.get("scale") or "major"
        genre = prompt_row.get("genre") or "Pop"

        lines = [f"[Global Metadata]\nbpm is {bpm}. key is {key}, and scale is {scale}. {genre}."]
        if "emotional_progression" in segs.get("global_metadata", {}):
            lines.append(f"Emotion: {segs['global_metadata']['emotional_progression']}")
        if "sonics_production" in segs.get("global_metadata", {}):
            lines.append(f"Production: {segs['global_metadata']['sonics_production']}")

        if segs.get("vocal_details"):
            lines.append("\n[Vocal Details]")
            for k, v in segs["vocal_details"].items():
                lines.append(f"{k}: {v}")

        if segs.get("arrangement"):
            lines.append("\n[Arrangement]")
            for k, v in segs["arrangement"].items():
                lines.append(f"{k}: {v}")

        return "\n".join(lines)

    @staticmethod
    def format_for_songbench(
        prompt_row: dict[str, Any], segs: dict[str, dict[str, str]], mood: str | None = None
    ) -> str:
        from songwriter.music.engine_prompt import render_songbench
        g = prompt_row.get("genre", "").strip()
        sg = prompt_row.get("subgenre", "").strip()
        if g and sg and sg.lower() not in g.lower():
            genre = f"{g} ({sg})"
        else:
            genre = g or sg or "Pop"
        bpm = prompt_row.get("bpm") or 90
        primary = segs.get("arrangement", {}).get("primary_layer", "")
        embellish = segs.get("arrangement", {}).get("embellishments", "") or segs.get("arrangement", {}).get("secondary_layer", "")
        groove = segs.get("arrangement", {}).get("groove_foundation", "")
        vocal = segs.get("vocal_details", {}).get("vocal_gender_timbre", prompt_row.get("vocal", "melodic vocal"))

        if not mood:
            emo = segs.get("global_metadata", {}).get("emotional_progression", "")
            if emo:
                m = re.search(r"(reflective|somber|celebratory|aggressive|uplifting|intimate|melancholic|energetic|nostalgic|dark|hopeful|triumphant|yearning|romantic|defiant|dreamy)", emo, re.I)
                mood = m.group(1).capitalize() if m else None

        return render_songbench(
            genre=genre,
            mood=mood,
            lead=primary,
            bed=embellish,
            groove=groove,
            vocal=vocal,
            bpm=bpm,
        )
