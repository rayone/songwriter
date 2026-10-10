"""Poetic form validators: deterministic structural checks.

The point of this module: VRSA-style tools pass form rules to a model as
prompt text and hope it complies. Every rule below is mechanically
checkable, so instead of hoping we return "line 3 has 8 syllables, needs 7".
A small model can fix a precise defect it could never have detected.

Each validator returns a FormReport whose violations are short, positional,
and actionable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from songwriter.craft.stats import CraftAnalyzer

_TOKEN = re.compile(r"[a-z']+")


@dataclass
class Violation:
    line: int | None          # 0-based line index, None = whole-form issue
    rule: str
    detail: str
    expected: str | None = None
    actual: str | None = None


@dataclass
class FormReport:
    form: str
    ok: bool
    line_count: int
    violations: list[Violation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.ok:
            return f"{self.form}: valid ({self.line_count} lines)"
        return (f"{self.form}: {len(self.violations)} violation(s) "
                f"in {self.line_count} lines")


def _norm(line: str) -> str:
    return " ".join(_TOKEN.findall(line.lower()))


class FormValidator:
    """Validates the structural forms exposed by the writing tools."""

    def __init__(self, analyzer: CraftAnalyzer | None = None) -> None:
        self.ca = analyzer or CraftAnalyzer()

    # -- helpers ------------------------------------------------------
    def _syllables(self, lines: list[str]) -> list[int]:
        return [self.ca.line_stat(l).syllables for l in lines]

    def _rime_keys(self, lines: list[str]) -> list[str | None]:
        return [self.ca.line_stat(l).rime_key for l in lines]

    def _scheme(self, lines: list[str]) -> str:
        return self.ca._scheme(self._rime_keys(lines))

    def _check_syllable_pattern(self, lines: list[str], pattern: list[int],
                                tol: int = 0) -> list[Violation]:
        out: list[Violation] = []
        syls = self._syllables(lines)
        for i, (got, want) in enumerate(zip(syls, pattern)):
            if abs(got - want) > tol:
                out.append(Violation(i, "syllable_count",
                                     f"line {i + 1} has {got} syllables, needs {want}",
                                     str(want), str(got)))
        return out

    def _check_scheme(self, lines: list[str], want: str) -> list[Violation]:
        """Compare rhyme-class structure, not literal letters.

        'AABBA' and 'BBAAB' describe the same structure, so we compare
        partitions of line indices rather than the letters themselves.
        """
        got = self._scheme(lines)
        out: list[Violation] = []
        if len(got) != len(want):
            out.append(Violation(None, "line_count",
                                 f"expected {len(want)} lines, got {len(got)}",
                                 str(len(want)), str(len(got))))
            return out

        want_groups: dict[str, list[int]] = {}
        got_groups: dict[str, list[int]] = {}
        for i, ch in enumerate(want):
            want_groups.setdefault(ch, []).append(i)
        for i, ch in enumerate(got):
            got_groups.setdefault(ch, []).append(i)

        want_part = sorted(tuple(v) for v in want_groups.values() if len(v) > 1)
        got_part = sorted(tuple(v) for v in got_groups.values() if len(v) > 1)
        if want_part != got_part:
            out.append(Violation(
                None, "rhyme_scheme",
                f"rhyme structure {got} does not match {want}", want, got))
        return out

    # -- strict syllable forms ----------------------------------------
    def haiku(self, lines: list[str]) -> FormReport:
        v: list[Violation] = []
        if len(lines) != 3:
            v.append(Violation(None, "line_count",
                               f"haiku needs 3 lines, got {len(lines)}", "3",
                               str(len(lines))))
        v += self._check_syllable_pattern(lines, [5, 7, 5])
        return FormReport("haiku", not v, len(lines), v)

    def cinquain(self, lines: list[str]) -> FormReport:
        v: list[Violation] = []
        if len(lines) != 5:
            v.append(Violation(None, "line_count",
                               f"cinquain needs 5 lines, got {len(lines)}", "5",
                               str(len(lines))))
        v += self._check_syllable_pattern(lines, [2, 4, 6, 8, 2])
        return FormReport("cinquain", not v, len(lines), v)

    def limerick(self, lines: list[str]) -> FormReport:
        v: list[Violation] = []
        if len(lines) != 5:
            v.append(Violation(None, "line_count",
                               f"limerick needs 5 lines, got {len(lines)}", "5",
                               str(len(lines))))
            return FormReport("limerick", False, len(lines), v)
        v += self._check_scheme(lines, "AABBA")
        syls = self._syllables(lines)
        # lines 3-4 are short; 1,2,5 long. Relative check, not absolute.
        long_avg = (syls[0] + syls[1] + syls[4]) / 3
        short_avg = (syls[2] + syls[3]) / 2
        if short_avg >= long_avg:
            v.append(Violation(None, "meter",
                               f"lines 3-4 (avg {short_avg:.1f} syl) should be "
                               f"shorter than lines 1,2,5 (avg {long_avg:.1f})"))
        return FormReport("limerick", not v, len(lines), v)

    def acrostic(self, lines: list[str], word: str) -> FormReport:
        v: list[Violation] = []
        target = word.upper()
        if len(lines) != len(target):
            v.append(Violation(None, "line_count",
                               f"acrostic on '{target}' needs {len(target)} lines, "
                               f"got {len(lines)}", str(len(target)),
                               str(len(lines))))
        for i, (line, ch) in enumerate(zip(lines, target)):
            first = next((c for c in line if c.isalpha()), "")
            if first.upper() != ch:
                v.append(Violation(i, "acrostic_letter",
                                   f"line {i + 1} starts with '{first}', needs '{ch}'",
                                   ch, first))
        return FormReport("acrostic", not v, len(lines), v)

    # -- complex forms ------------------------------------------------
    def sonnet(self, lines: list[str], scheme: str = "ABABCDCDEFEFGG") -> FormReport:
        v: list[Violation] = []
        if len(lines) != 14:
            v.append(Violation(None, "line_count",
                               f"sonnet needs 14 lines, got {len(lines)}", "14",
                               str(len(lines))))
            return FormReport("sonnet", False, len(lines), v)
        v += self._check_scheme(lines, scheme)
        # iambic pentameter ~10 syllables; allow 9-11 for singability
        for i, s in enumerate(self._syllables(lines)):
            if not 9 <= s <= 11:
                v.append(Violation(i, "meter",
                                   f"line {i + 1} has {s} syllables, "
                                   f"iambic pentameter wants 10", "10", str(s)))
        return FormReport("sonnet", not v, len(lines), v)

    def villanelle(self, lines: list[str]) -> FormReport:
        """19 lines. Line 1 repeats at 6, 12, 18; line 3 repeats at 9, 15, 19
        (1-based). Refrain positions are exact, so this is fully checkable.
        """
        v: list[Violation] = []
        if len(lines) != 19:
            v.append(Violation(None, "line_count",
                               f"villanelle needs 19 lines, got {len(lines)}",
                               "19", str(len(lines))))
            return FormReport("villanelle", False, len(lines), v)

        a1, a2 = _norm(lines[0]), _norm(lines[2])
        for pos in (5, 11, 17):          # 0-based for lines 6, 12, 18
            if _norm(lines[pos]) != a1:
                v.append(Violation(pos, "refrain",
                                   f"line {pos + 1} must repeat line 1"))
        for pos in (8, 14, 18):          # 0-based for lines 9, 15, 19
            if _norm(lines[pos]) != a2:
                v.append(Violation(pos, "refrain",
                                   f"line {pos + 1} must repeat line 3"))
        return FormReport("villanelle", not v, len(lines), v)

    def sestina(self, lines: list[str]) -> FormReport:
        """39 lines: six 6-line stanzas plus a 3-line envoi. The six end-words
        rotate by a fixed permutation (6,1,5,2,4,3).
        """
        v: list[Violation] = []
        if len(lines) != 39:
            v.append(Violation(None, "line_count",
                               f"sestina needs 39 lines, got {len(lines)}", "39",
                               str(len(lines))))
            return FormReport("sestina", False, len(lines), v)

        def end(i: int) -> str:
            toks = _TOKEN.findall(lines[i].lower())
            return toks[-1] if toks else ""

        first = [end(i) for i in range(6)]
        if len(set(first)) != 6:
            v.append(Violation(None, "end_words",
                               "stanza 1 must use six distinct end-words"))
            return FormReport("sestina", False, len(lines), v)

        order = list(range(6))
        for st in range(1, 6):
            order = [order[5], order[0], order[4], order[1], order[3], order[2]]
            want = [first[i] for i in order]
            got = [end(st * 6 + k) for k in range(6)]
            for k, (w, g) in enumerate(zip(want, got)):
                if w != g:
                    ln = st * 6 + k
                    v.append(Violation(ln, "end_word_rotation",
                                       f"line {ln + 1} must end with '{w}', "
                                       f"got '{g}'", w, g))
        return FormReport("sestina", not v, len(lines), v)

    def pantoum(self, lines: list[str]) -> FormReport:
        """Quatrains chained: lines 2 and 4 of a stanza become lines 1 and 3
        of the next.
        """
        v: list[Violation] = []
        if len(lines) % 4 or len(lines) < 8:
            v.append(Violation(None, "line_count",
                               f"pantoum needs whole quatrains (>=8), "
                               f"got {len(lines)}"))
            return FormReport("pantoum", False, len(lines), v)

        stanzas = [lines[i:i + 4] for i in range(0, len(lines), 4)]
        for si in range(1, len(stanzas)):
            prev, cur = stanzas[si - 1], stanzas[si]
            base = si * 4
            if _norm(cur[0]) != _norm(prev[1]):
                v.append(Violation(base, "chain",
                                   f"line {base + 1} must repeat line "
                                   f"{base - 2}"))
            if _norm(cur[2]) != _norm(prev[3]):
                v.append(Violation(base + 2, "chain",
                                   f"line {base + 3} must repeat line {base}"))
        return FormReport("pantoum", not v, len(lines), v)

    def ghazal(self, lines: list[str]) -> FormReport:
        """Couplets. Both lines of couplet 1 rhyme; after that only the
        second line of each couplet carries the rhyme.
        """
        v: list[Violation] = []
        if len(lines) % 2 or len(lines) < 4:
            v.append(Violation(None, "line_count",
                               f"ghazal needs whole couplets (>=4), "
                               f"got {len(lines)}"))
            return FormReport("ghazal", False, len(lines), v)

        keys = self._rime_keys(lines)
        anchor = keys[0]
        if not anchor:
            v.append(Violation(0, "rhyme", "cannot determine rhyme of line 1"))
            return FormReport("ghazal", False, len(lines), v)
        if keys[1] != anchor:
            v.append(Violation(1, "rhyme", "line 2 must rhyme with line 1"))
        for ci in range(1, len(lines) // 2):
            idx = ci * 2 + 1
            if keys[idx] != anchor:
                v.append(Violation(idx, "rhyme",
                                   f"line {idx + 1} must carry the ghazal rhyme"))
        return FormReport("ghazal", not v, len(lines), v)

    def ballad(self, lines: list[str]) -> FormReport:
        """Quatrains rhyming ABCB."""
        v: list[Violation] = []
        if len(lines) % 4 or not lines:
            v.append(Violation(None, "line_count",
                               f"ballad needs whole quatrains, got {len(lines)}"))
            return FormReport("ballad", False, len(lines), v)
        keys = self._rime_keys(lines)
        for si in range(0, len(lines), 4):
            if keys[si + 1] is None or keys[si + 3] is None:
                v.append(Violation(si + 3, "rhyme",
                                   f"lines {si + 2} and {si + 4} must rhyme"))
            elif keys[si + 1] != keys[si + 3]:
                v.append(Violation(si + 3, "rhyme",
                                   f"line {si + 4} must rhyme with line {si + 2} "
                                   "(ABCB)"))
        return FormReport("ballad", not v, len(lines), v)

    # -- generic scheme check -----------------------------------------
    def scheme(self, lines: list[str], want: str) -> FormReport:
        """Validate against an arbitrary scheme such as AABB or ABBA."""
        v = self._check_scheme(lines, want)
        return FormReport(f"scheme:{want}", not v, len(lines), v)

    def couplets(self, lines: list[str]) -> FormReport:
        return self.scheme(lines, "AABB" * (len(lines) // 4) or "AA")

    # -- dispatch -----------------------------------------------------
    def validate(self, form: str, lines: list[str], **kw) -> FormReport:
        form = form.lower().strip()
        table = {
            "haiku": self.haiku,
            "cinquain": self.cinquain,
            "limerick": self.limerick,
            "sonnet": self.sonnet,
            "villanelle": self.villanelle,
            "sestina": self.sestina,
            "pantoum": self.pantoum,
            "ghazal": self.ghazal,
            "ballad": self.ballad,
        }
        if form == "acrostic":
            return self.acrostic(lines, kw.get("word", ""))
        if form.startswith("scheme:"):
            return self.scheme(lines, form.split(":", 1)[1].upper())
        fn = table.get(form)
        if fn is None:
            # Forms with no mechanical constraint (free verse, ode, elegy,
            # pastoral, narrative, ekphrastic, prose) are advisory only.
            return FormReport(form, True, len(lines), [],
                              [f"'{form}' has no mechanical constraints to check"])
        return fn(lines)


FREE_FORMS = {"free verse", "free_verse", "narrative", "elegy", "ode",
              "pastoral", "ekphrastic", "prose"}
