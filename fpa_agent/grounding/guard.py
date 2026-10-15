"""
Grounding guard — pure functions (no ADK imports, fully unit-testable).

Ported from the Kaplan build's grounding/callbacks.py, keeping every
production-learned rule, and generalised:

  * numbers in the final answer must match a number some tool returned this
    turn (or the user stated, or the prompt's calendar facts), allowing only
    honest restatements: coarser rounding, K/M/B abbreviation, a ratio shown as
    a percentage, or the exact sum/difference of two grounded numbers;
  * table rows in a dimensional breakdown must be labels a tool returned —
    the header keywords now come from the client's dimensions in the semantic
    layer instead of a hard-coded "channel/modality";
  * a figure grounded ONLY by exploratory (non-semantic-layer) queries must be
    labelled exploratory in the answer;
  * an answer may not claim "no data" when a query in the same turn returned
    data;
  * every grounded number is ATTRIBUTED to evidence ids — the audit trail.
"""
from __future__ import annotations

import calendar
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable

# A digit run touching a letter is part of a token (e.g. "B2B", "Q3"), not a
# number — except a K/M/B scale suffix directly on the number ("$227.95K").
NUM_RE = re.compile(r"(?<![A-Za-z])-?\d[\d,]*(?:\.\d+)?%?(?:[KkMmBb](?![A-Za-z]))?(?![A-Za-z])")
_QUARTER_RE = re.compile(r"\b(?:FY\s?\d{2,4}\s?)?Q[1-4]\b|\bH[12]\b|\bFY\s?\d{2,4}\b")
_LIST_MARKER_RE = re.compile(r"(?m)^\s*\d+\.\s")
_NUMBER_WORDS = {
    "zero": "0", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
    "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12",
    "thirteen": "13", "fourteen": "14", "fifteen": "15", "sixteen": "16",
    "seventeen": "17", "eighteen": "18", "nineteen": "19", "twenty": "20",
}
_NUMBER_WORD_RE = re.compile(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b", re.IGNORECASE)
_SCALES = (1_000, 1_000_000, 1_000_000_000)
_TABLE_ROW_RE = re.compile(r"^[ \t]*\|[ \t]*\*{0,2}([^|*]+?)\*{0,2}[ \t]*\|")
_SEPARATOR_ROW_RE = re.compile(r"^[ \t]*\|[ \t:|-]+\|[ \t]*$")
EXPLORATORY_DISCLAIMER_RE = re.compile(r"exploratory|preliminary|not an official|unofficial", re.IGNORECASE)
DECLINE_RE = re.compile(
    r"(unable to (provide|retrieve|confirm|answer)|(does|do)\s?n[o']?t (contain|have)|"
    r"(cannot|can[o']?t|couldn[o']?t) (provide|retrieve|find)|no data (is )?available|"
    r"not (currently )?available)", re.IGNORECASE)


def normalize(tok: str) -> str:
    if tok and tok[-1] in "KkMmBb":
        tok = tok[:-1]
    return tok.replace(",", "").rstrip("%")


def extract_numbers(text: str) -> set[str]:
    text = _QUARTER_RE.sub(" ", text or "")
    text = _LIST_MARKER_RE.sub("", text)
    digits = {normalize(m) for m in NUM_RE.findall(text)}
    words = {_NUMBER_WORDS[w.lower()] for w in _NUMBER_WORD_RE.findall(text)}
    out = set()
    for d in digits | words:
        if d in ("", "-"):
            continue
        out.add(d)
        out.add(d.lstrip("-"))
    return out


def numbers_in_payload(payload: Any) -> set[str]:
    """Numbers from a tool payload. Strings are scanned too (tables, SQL)."""
    text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    return extract_numbers(text)


def labels_in_payload(payload: Any) -> set[str]:
    """Short string values that appear as category labels in tool output:
    values inside lists of dicts (rows, drivers) and lists of strings."""
    found: set[str] = set()

    def walk(x):
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for item in x:
                if isinstance(item, dict):
                    for v in item.values():
                        if isinstance(v, str) and 0 < len(v.strip()) <= 80:
                            found.add(v.strip().lower())
                        else:
                            walk(v)
                elif isinstance(item, str) and 0 < len(item.strip()) <= 80:
                    found.add(item.strip().lower())
                else:
                    walk(item)

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return found
    walk(payload)
    return found


def is_benign(tok: str) -> bool:
    bare = tok.lstrip("-")
    return bool(re.fullmatch(r"(19|20)\d{2}", bare)) or (bare.startswith("0") and len(bare) >= 4 and "." not in bare)


def _decimals(tok: str) -> int:
    return len(tok.split(".", 1)[1]) if "." in tok else 0


def _floats(allow: Iterable[str]) -> list[float]:
    out = []
    for a in allow:
        try:
            out.append(float(a))
        except ValueError:
            pass
    return out


class Matcher:
    """Precomputes the allow-list once per grounding pass."""

    def __init__(self, allow: set[str]):
        self.allow = allow
        self.nums = _floats(allow)
        self._pairs: list[float] | None = None
        self._buckets: dict[int, set[float]] = {}

    def _pair_bucket(self, decimals: int) -> set[float]:
        if self._pairs is None:
            n, pairs = self.nums, []
            if len(n) <= 400:  # bounded: n^2 pairs
                for i in range(len(n)):
                    for j in range(i, len(n)):
                        pairs += [n[i] + n[j], n[i] - n[j], n[j] - n[i]]
            self._pairs = pairs
        if decimals not in self._buckets:
            self._buckets[decimals] = {round(v, decimals) for v in self._pairs}
        return self._buckets[decimals]

    def match_kind(self, tok: str) -> str | None:
        """How `tok` is grounded, or None. Kinds: exact, rounded, scaled,
        percent, combination."""
        if tok in self.allow:
            return "exact"
        try:
            c = float(tok)
        except ValueError:
            return None
        d = _decimals(tok)
        precisions = {d} | ({d - 1} if d > 0 else set())
        for a in self.nums:
            for p in precisions:
                if round(a, p) == round(c, p):
                    return "rounded"
        for a in self.nums:
            for s in _SCALES:
                # "$0.66M" for 659,542.71 is a normal way to write it
                if abs(a) >= s / 100 and round(a / s, d) == round(c, d):
                    return "scaled"
            if abs(a) < 10 and round(a * 100, d) == round(c, d):
                return "percent"
        if round(c, d) in self._pair_bucket(d):
            return "combination"
        return None


def calendar_numbers(today: date | None = None, extra: Iterable[str] = ()) -> set[str]:
    """Date facts the prompt itself injects (today, last closed month)."""
    now = today or datetime.now().date()
    y, m = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    return {"1", str(now.year), str(now.month), str(now.day), str(y), str(m),
            str(calendar.monthrange(y, m)[1]), *extra}


def offending_table_labels(text: str, allow_labels: set[str], header_keywords: set[str]) -> list[str]:
    if not allow_labels or not header_keywords:
        return []
    bad, lines = [], text.splitlines()
    for i, line in enumerate(lines):
        if not _SEPARATOR_ROW_RE.match(line) or i == 0:
            continue
        hm = _TABLE_ROW_RE.match(lines[i - 1])
        header = hm.group(1).strip().lower() if hm else ""
        if not any(k in header for k in header_keywords):
            continue
        for row in lines[i + 1:]:
            m = _TABLE_ROW_RE.match(row)
            if not m:
                break
            label = m.group(1).strip()
            norm = label.lower()
            if not norm or norm in ("total", "grand total", "net change", "change"):
                continue
            if any(norm == a or norm in a or a in norm for a in allow_labels):
                continue
            bad.append(label)
    return bad


@dataclass
class Capture:
    """Numbers/labels one tool call contributed, with the evidence behind them."""
    numbers: list[str]
    labels: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    tool: str = ""
    exploratory: bool = False
    returned_data: bool = False


@dataclass
class Verdict:
    ok: bool
    offending_numbers: list[str] = field(default_factory=list)
    offending_labels: list[str] = field(default_factory=list)
    unlabelled_exploratory: list[str] = field(default_factory=list)
    contradicts_data: bool = False
    attribution: dict[str, dict] = field(default_factory=dict)   # number -> {kind, evidence_ids, tools}

    def message(self) -> str:
        parts = []
        if self.offending_numbers:
            parts.append(f"number(s) not traceable to any tool result: {self.offending_numbers}")
        if self.offending_labels:
            parts.append(f"row label(s) not returned by any tool: {self.offending_labels}")
        if self.unlabelled_exploratory:
            parts.append(f"exploratory figure(s) presented as official: {self.unlabelled_exploratory}")
        if self.contradicts_data:
            parts.append("the answer says no data is available, but a query this turn returned data")
        return "[grounding guard] Response withheld - it contained " + "; ".join(parts) + "."


def check(text: str, captures: list[Capture], user_numbers: set[str],
          header_keywords: set[str], today: date | None = None,
          calendar_extra: Iterable[str] = ()) -> Verdict:
    official = [c for c in captures if not c.exploratory]
    exploratory = [c for c in captures if c.exploratory]
    official_allow = set(user_numbers) | {n for c in official for n in c.numbers}
    explore_allow = {n for c in exploratory for n in c.numbers}
    cal = calendar_numbers(today, calendar_extra)
    m_official, m_all = Matcher(official_allow), Matcher(official_allow | explore_allow)

    verdict = Verdict(ok=True)
    for tok in sorted(extract_numbers(text)):
        if tok in cal or is_benign(tok):
            continue
        kind = m_official.match_kind(tok)
        if kind:
            verdict.attribution[tok] = _attribute(tok, official, kind)
            continue
        kind = m_all.match_kind(tok)
        if kind:
            verdict.attribution[tok] = _attribute(tok, exploratory, kind)
            if not EXPLORATORY_DISCLAIMER_RE.search(text):
                verdict.unlabelled_exploratory.append(tok)
            continue
        if tok.lstrip("-") != tok and tok.lstrip("-") in verdict.attribution:
            continue
        verdict.offending_numbers.append(tok)

    allow_labels = {l for c in captures for l in c.labels}
    verdict.offending_labels = offending_table_labels(text, allow_labels, header_keywords)
    # Scoped (as in the Kaplan build) to exploratory queries that returned rows:
    # an honest decline for an ungoverned metric must still be possible.
    verdict.contradicts_data = any(c.exploratory and c.returned_data for c in captures) \
        and bool(DECLINE_RE.search(text))
    # a sign-only variant of a grounded number is fine ("-312" vs "312")
    verdict.offending_numbers = [t for t in verdict.offending_numbers
                                 if t.lstrip("-") not in verdict.attribution]
    verdict.ok = not (verdict.offending_numbers or verdict.offending_labels
                      or verdict.unlabelled_exploratory or verdict.contradicts_data)
    return verdict


def _attribute(tok: str, captures: list[Capture], kind: str) -> dict:
    hits = [c for c in captures if Matcher(set(c.numbers)).match_kind(tok)] or captures
    return {
        "kind": kind,
        "evidence_ids": sorted({e for c in hits for e in c.evidence_ids}),
        "tools": sorted({c.tool for c in hits if c.tool}),
    }
