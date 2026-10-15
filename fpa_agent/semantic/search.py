"""
Term resolution over the whole semantic layer (metrics, analyses, glossary,
dimensions). Deterministic tiers first — exact id/term, exact alias token,
whole-phrase containment, token overlap, fuzzy — so the same words always
resolve to the same row. Results carry the governance status so the agent
knows immediately whether a figure can be produced.

An optional closed-set LLM fallback (pick one of the existing terms or NONE)
can be plugged in via `set_llm_fallback`; it can only ever return an id that
already exists in the catalog.
"""
from __future__ import annotations

import difflib
import re
from typing import Callable

from fpa_agent.semantic.repository import Catalog

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "of", "for", "in", "by", "and", "to", "is", "are", "was", "what",
         "why", "how", "did", "do", "does", "vs", "versus", "our", "my", "me", "show", "give",
         "last", "this", "year", "month", "quarter", "fy", "ytd", "q1", "q2", "q3", "q4",
         "up", "down", "drove", "driving", "drive", "change", "changed", "compared", "with"}

_llm_fallback: Callable[[str, list[tuple[str, str]]], str | None] | None = None


def set_llm_fallback(fn: Callable[[str, list[tuple[str, str]]], str | None] | None) -> None:
    global _llm_fallback
    _llm_fallback = fn


def _norm(s: str) -> str:
    return " ".join(_WORD_RE.findall((s or "").lower()))


def _tokens(s: str) -> set[str]:
    return {t for t in _WORD_RE.findall((s or "").lower()) if t not in _STOP}


def _score(query: str, names: list[str]) -> int:
    q = _norm(query)
    if not q:
        return 0
    normed = [_norm(n) for n in names if n]
    if q in normed[:2]:                       # id or term
        return 100
    if q in normed:
        return 95
    best = 0
    for n in normed:
        if n and re.search(rf"\b{re.escape(n)}\b", q):
            best = max(best, 80 + min(len(n.split()), 5))   # longer phrases win
    if best:
        return best
    qt = _tokens(q)
    for n in normed:
        nt = _tokens(n)
        if qt and nt:
            overlap = len(qt & nt) / len(nt)
            if overlap == 1:
                best = max(best, 70)
            elif overlap >= 0.5:
                best = max(best, 55)
        ratio = difflib.SequenceMatcher(None, q, n).ratio()
        if ratio >= 0.85:
            best = max(best, 50)
    return best


def search(catalog: Catalog, query: str, limit: int = 8) -> list[dict]:
    hits: list[dict] = []
    for m in catalog.metrics.values():
        s = _score(query, [m.metric_id.replace("_", " "), m.term, *m.aliases])
        if s:
            hits.append({
                "type": "metric", "id": m.metric_id, "name": m.term, "score": s,
                "definition": m.definition, "unit": m.unit, "status": m.definition_status,
                "category": m.category, "queryable": m.is_queryable,
                "dimensions": catalog.dimensions_of(m.metric_id),
                "analyses": [a.analysis_id for a in catalog.analyses_for_metric(m.metric_id)
                             if a.status != "roadmap"],
                "owner": m.owner,
            })
    for a in catalog.analyses.values():
        s = max(_score(query, [a.analysis_id.replace("_", " "), a.title]),
                max((_score(query, [q]) for q in a.question_examples), default=0) - 10)
        if s:
            hits.append({"type": "analysis", "id": a.analysis_id, "name": a.title, "score": s,
                         "headline_metric": a.headline_metric, "method": a.method,
                         "status": a.status, "description": a.description})
    for g in catalog.glossary.values():
        s = _score(query, [g.term, g.term, *g.aliases])
        if s:
            hits.append({"type": "glossary", "id": g.term, "name": g.term, "score": s,
                         "definition": g.definition, "related_metrics": g.related_metrics})
    for d in catalog.dimensions.values():
        s = _score(query, [d.dimension_id.replace("_", " "), d.display_name, *d.aliases])
        if s:
            hits.append({"type": "dimension", "id": d.dimension_id, "name": d.display_name,
                         "score": s, "description": d.description, "values_hint": d.values_hint})
    hits.sort(key=lambda h: (-h["score"], h["type"] != "metric", h["id"]))
    if not hits and _llm_fallback is not None:
        picked = _llm_fallback(query, [(m.metric_id, f"{m.term}: {m.definition}") for m in catalog.metrics.values()])
        if picked in catalog.metrics:
            return search(catalog, catalog.metrics[picked].term, limit)
    return hits[:limit]
