"""
The agent's tools — a small, GENERIC set that works for any client:

  search_semantic_layer   find the governed metric / analysis / glossary term
  get_definition          full definition, owner, status, dimensions
  get_metric              a value, a breakdown, or a monthly trend
  compare_metric          vs prior year / prior period / budget (+ breakdown)
  run_analysis            a configured driver analysis (netting-guaranteed)
  list_dimension_values   valid category values for a filter
  calculate               arithmetic on numbers ALREADY returned this turn
  explain_lineage         where a number came from (evidence -> SQL -> source)
  data_explorer tools     read-only, explicitly exploratory investigation

Each fetch/compute tool is ONE deterministic hop: semantic layer -> compiled
query -> source -> Decimal math -> evidence. No nested agents on the number
path (the latency lesson from the Kaplan build), and no client-specific tool.

Periods are passed as flat parameters (more reliable for function calling
than nested objects) and resolved by fpa_agent.periods — never by the model.
"""
from __future__ import annotations

import re
from typing import Any

from fpa_agent.connectors import registry as connectors
from fpa_agent.connectors.base import DataSourceUnavailableError
from fpa_agent.engine.formulas import FormulaError, evaluate
from fpa_agent.engine.methods import METHODS, MethodError
from fpa_agent.engine.results import D, quantize
from fpa_agent.engine.service import GateError, MetricService, QueryError
from fpa_agent.grounding import callbacks as gcb
from fpa_agent.grounding import guard
from fpa_agent.grounding.evidence import Evidence, EvidenceLedger, new_id
from fpa_agent.query.compiler import QueryCompileError
from fpa_agent.semantic import search as sl_search
from fpa_agent.semantic.repository import get_catalog

try:  # ADK is optional for the engine; tools accept tool_context=None in tests.
    from google.adk.tools import ToolContext
except ImportError:  # pragma: no cover
    ToolContext = Any  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# Plumbing
# ─────────────────────────────────────────────────────────────────────────────

def _period(period_type, year, quarter, month, offset, fiscal, start_year, start_month,
            end_year, end_month, n) -> dict:
    return {k: v for k, v in {
        "type": period_type, "year": year, "quarter": quarter, "month": month, "offset": offset,
        "fiscal": fiscal, "start_year": start_year, "start_month": start_month,
        "end_year": end_year, "end_month": end_month, "n": n,
    }.items() if v is not None}


def _filters(filters: list[str] | None) -> dict[str, list[str]]:
    """["program=Nursing|Business", "modality=Online"] -> {"program": [...], ...}"""
    out: dict[str, list[str]] = {}
    for f in filters or []:
        if "=" not in f:
            raise QueryError(f"filter {f!r} must look like 'dimension=value' or 'dimension=v1|v2'")
        dim, vals = f.split("=", 1)
        out.setdefault(dim.strip(), []).extend(v.strip() for v in vals.split("|") if v.strip())
    return out


def _persist(ledger: EvidenceLedger, tool_context) -> None:
    if tool_context is None:
        return
    for ev in ledger.all():
        tool_context.state[f"evidence:{ev.evidence_id}"] = ev.model_dump(mode="json")


async def _guarded(tool_context, fn, *args, **kwargs) -> dict:
    ledger = EvidenceLedger()
    service = MetricService(get_catalog(), ledger)
    try:
        result = await fn(service, *args, **kwargs)
        return {"status": "ok", **result}
    except GateError as e:
        return {"status": "declined", "reason": str(e), "definition_status": e.status, "owner": e.owner}
    except DataSourceUnavailableError as e:
        return {"status": "unavailable",
                "reason": "A source system did not respond after two attempts. This is a temporary "
                          "connectivity issue, not a data problem - please try again shortly.",
                "_debug_detail": str(e)[:300]}
    except (QueryError, MethodError, QueryCompileError, FormulaError, KeyError, ValueError) as e:
        return {"status": "error", "reason": str(e).strip("'\"")}
    finally:
        _persist(ledger, tool_context)


# ─────────────────────────────────────────────────────────────────────────────
# Semantic layer tools
# ─────────────────────────────────────────────────────────────────────────────

async def search_semantic_layer(query: str) -> dict:
    """Find governed metrics, analyses, glossary terms and dimensions matching a
    business phrase (e.g. "starts", "revenue vs budget", "census"). Call this
    whenever the metric the user means is not obvious from the catalogue in
    your instructions. Use the returned `id` values in later tool calls.

    Args:
        query: the user's own wording for the concept.
    """
    hits = sl_search.search(get_catalog(), query)
    return {"status": "ok", "query": query, "matches": hits,
            "note": None if hits else "Nothing in the semantic layer matches; this is not a governed metric."}


async def get_definition(name: str) -> dict:
    """Full semantic-layer record for a metric id, analysis id, glossary term or
    dimension id: definition, owner, status, open questions, dimensions,
    supported comparisons. Use for "what does X mean / how is X calculated".

    Args:
        name: a metric_id, analysis_id, dimension_id or glossary term.
    """
    cat = get_catalog()
    if name in cat.metrics:
        m = cat.metrics[name]
        return {"status": "ok", "type": "metric", "metric_id": m.metric_id, "term": m.term,
                "definition": m.definition, "business_context": m.business_context,
                "unit": m.unit, "favorable_direction": m.favorable_direction,
                "definition_status": m.definition_status, "owner": m.owner,
                "open_questions": m.open_questions or None, "category": m.category,
                "calculation": m.formula if m.metric_type == "derived" else m.measure,
                "source_system": cat.sources[m.source_id].display_name
                if m.source_id in cat.sources else None,
                "filters": [f.model_dump() for f in m.filters],
                "time_semantics": m.time_semantics, "dimensions": cat.dimensions_of(m.metric_id),
                "has_budget": bool(m.budget_metric_id),
                "analyses": [a.analysis_id for a in cat.analyses_for_metric(m.metric_id)]}
    if name in cat.analyses:
        a = cat.analyses[name]
        spec = METHODS.get(a.method)
        return {"status": "ok", "type": "analysis", **a.model_dump(exclude={"notes"}),
                "method_description": spec.description if spec else None,
                "supported_comparisons": list(spec.comparisons) if spec else []}
    if name in cat.dimensions:
        return {"status": "ok", "type": "dimension", **cat.dimensions[name].model_dump()}
    for g in cat.glossary.values():
        if g.term.lower() == name.lower() or name.lower() in [x.lower() for x in g.aliases]:
            return {"status": "ok", "type": "glossary", **g.model_dump()}
    return {"status": "error", "reason": f"{name!r} is not in the semantic layer; try search_semantic_layer."}


# ─────────────────────────────────────────────────────────────────────────────
# Number tools
# ─────────────────────────────────────────────────────────────────────────────

async def get_metric(metric_id: str, period_type: str, year: int | None = None,
                     quarter: int | None = None, month: int | None = None, offset: int | None = None,
                     fiscal: bool | None = None, start_year: int | None = None,
                     start_month: int | None = None, end_year: int | None = None,
                     end_month: int | None = None, n: int | None = None,
                     group_by: list[str] | None = None, filters: list[str] | None = None,
                     monthly_trend: bool = False, tool_context: ToolContext = None) -> dict:
    """The value of ONE governed metric for a period - optionally broken down by
    dimensions, filtered, or as a monthly trend. Use for "what was X" questions.

    Args:
        metric_id: id from the semantic layer (e.g. "net_revenue").
        period_type: month | quarter | year | ytd | qtd | range | last_n_months.
        year: explicit year as the user said it (fiscal-year label when fiscal=true).
        quarter: 1-4. month: 1-12 (for ytd/qtd: the through-month).
        offset: relative periods from the current one (0 current, -1 previous) when no explicit year.
        fiscal: true for fiscal quarters/years/YTD (the client's fiscal calendar).
        start_year/start_month/end_year/end_month: for period_type="range".
        n: number of months for period_type="last_n_months".
        group_by: dimension ids to break down by (e.g. ["program"]).
        filters: ["dimension=value"] or ["dimension=v1|v2"].
        monthly_trend: true to return one value per month in the period.
    """
    p = _period(period_type, year, quarter, month, offset, fiscal, start_year, start_month, end_year, end_month, n)
    return await _guarded(tool_context, lambda s: s.get_metric(
        metric_id, p, _filters(filters), group_by or [], "month" if monthly_trend else None))


async def compare_metric(metric_id: str, period_type: str, comparison: str = "prior_year",
                         year: int | None = None, quarter: int | None = None, month: int | None = None,
                         offset: int | None = None, fiscal: bool | None = None,
                         start_year: int | None = None, start_month: int | None = None,
                         end_year: int | None = None, end_month: int | None = None, n: int | None = None,
                         breakdown: str | None = None, filters: list[str] | None = None,
                         tool_context: ToolContext = None) -> dict:
    """Compare ONE metric with prior_year, prior_period or budget, with the
    change, % change and favorability; optionally attribute the change to the
    categories of one dimension (breakdown). Use when no configured analysis
    fits, or for a simple "X vs last year/budget" question.

    Args:
        metric_id: id from the semantic layer.
        period_type, year, quarter, month, offset, fiscal, start_*, end_*, n: as in get_metric.
        comparison: prior_year | prior_period | budget.
        breakdown: one dimension id to attribute the change to (e.g. "modality").
        filters: ["dimension=value"].
    """
    p = _period(period_type, year, quarter, month, offset, fiscal, start_year, start_month, end_year, end_month, n)
    return await _guarded(tool_context, lambda s: s.compare_metric(
        metric_id, p, comparison, _filters(filters), breakdown))


async def run_analysis(analysis_id: str, period_type: str, comparison: str | None = None,
                       year: int | None = None, quarter: int | None = None, month: int | None = None,
                       offset: int | None = None, fiscal: bool | None = None,
                       start_year: int | None = None, start_month: int | None = None,
                       end_year: int | None = None, end_month: int | None = None, n: int | None = None,
                       breakdown: str | None = None, filters: list[str] | None = None,
                       tool_context: ToolContext = None) -> dict:
    """Run a configured driver analysis ("why did X change", "what drove X") and
    return the headline change plus drivers that are PROVEN to sum exactly to it.
    Present `drivers_table` verbatim.

    Args:
        analysis_id: id from the analyses catalogue.
        period_type, year, quarter, month, offset, fiscal, start_*, end_*, n: as in get_metric.
        comparison: prior_year | prior_period | budget (omit for the analysis default).
        breakdown: optional dimension override for dimension-based methods.
        filters: ["dimension=value"] to scope the whole analysis.
    """
    p = _period(period_type, year, quarter, month, offset, fiscal, start_year, start_month, end_year, end_month, n)
    return await _guarded(tool_context, lambda s: s.run_analysis(
        analysis_id, p, comparison, _filters(filters), breakdown))


async def list_dimension_values(metric_id: str, dimension: str, tool_context: ToolContext = None) -> dict:
    """The valid values of a dimension for a metric (e.g. all programs), to build
    filters or answer "which channels do we have".

    Args:
        metric_id: id from the semantic layer.
        dimension: dimension id.
    """
    return await _guarded(tool_context, lambda s: s.list_dimension_values(metric_id, dimension))


_NAMED_VALUE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(-?[\d,]*\.?\d+)\s*$")


async def calculate(expression: str, values: list[str], tool_context: ToolContext = None) -> dict:
    """Exact Decimal arithmetic on numbers that tools ALREADY returned in this
    conversation turn - e.g. a share, a sum of two drivers, a growth rate the
    tools did not provide. Never do arithmetic in your head.

    Args:
        expression: arithmetic over named values, e.g. "({a} - {b}) / {b} * 100".
        values: ["a=1234.5", "b=1100"] - every number must have come from a tool this turn.
    """
    named: dict[str, Any] = {}
    for v in values:
        m = _NAMED_VALUE_RE.match(v)
        if not m:
            return {"status": "error", "reason": f"value {v!r} must look like name=number"}
        named[m.group(1)] = m.group(2).replace(",", "")
    if tool_context is not None:
        allow = {n for c in gcb.captures_in_window(tool_context.state) for n in c.numbers}
        allow |= gcb.user_numbers_in_window(tool_context.state)
        matcher = guard.Matcher(allow)
        ungrounded = [f"{k}={v}" for k, v in named.items() if not matcher.match_kind(v.lstrip("-"))]
        if ungrounded:
            return {"status": "declined",
                    "reason": f"these inputs were not returned by any tool this turn: {ungrounded}. "
                              "Fetch them first."}
    try:
        result = evaluate(expression, {k: D(v) for k, v in named.items()})
    except FormulaError as e:
        return {"status": "error", "reason": str(e)}
    ev_id = new_id()
    if tool_context is not None:
        tool_context.state[f"evidence:{ev_id}"] = Evidence(
            evidence_id=ev_id, kind="calculation", formula=expression,
            values={"result": str(result), **named}).model_dump(mode="json")
    return {"status": "ok", "expression": expression, "inputs": named,
            "result": quantize(result, 4) if result is not None else None,
            "result_2dp": quantize(result, 2) if result is not None else None, "evidence_id": ev_id}


async def explain_lineage(evidence_id: str, tool_context: ToolContext = None) -> dict:
    """Explain exactly where a figure came from: the analysis, the metric
    definitions (and their status/owner), the source system, the period and the
    exact SQL executed. Use when the user asks "how was this calculated", "where
    does this number come from" or "show me the query".

    Args:
        evidence_id: the evidence_id returned by get_metric/compare_metric/run_analysis.
    """
    if tool_context is None:
        return {"status": "error", "reason": "no session state available"}
    state = tool_context.state

    def lookup(eid):
        raw = state.get(f"evidence:{eid}")
        return Evidence(**raw) if raw else None

    chain = EvidenceLedger().lineage(evidence_id, lookup)
    if not chain:
        return {"status": "error", "reason": f"unknown evidence id {evidence_id!r} in this session"}
    cat = get_catalog()
    steps = []
    for ev in chain:
        step = {"evidence_id": ev.evidence_id, "kind": ev.kind,
                "period": (ev.period or {}).get("label"), "values": ev.values}
        if ev.metric_id and ev.metric_id in cat.metrics:
            m = cat.metrics[ev.metric_id]
            step["metric"] = {"id": m.metric_id, "term": m.term, "definition": m.definition,
                              "status": m.definition_status, "owner": m.owner}
        if ev.source_id and ev.source_id in cat.sources:
            step["source_system"] = cat.sources[ev.source_id].display_name
        if ev.executed_sql:
            step["executed_sql"] = ev.executed_sql
        if ev.formula:
            step["formula"] = ev.formula
        if ev.analysis_id:
            step["analysis"] = ev.analysis_id
        steps.append(step)
    return {"status": "ok", "evidence_id": evidence_id, "lineage": steps}


# ─────────────────────────────────────────────────────────────────────────────
# Exploration tools (data_explorer sub-agent only) — read-only, labelled.
# ─────────────────────────────────────────────────────────────────────────────

_FROM_RE = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_.]*)", re.IGNORECASE)


def _check_tables(source_id: str, sql: str) -> None:
    allowed = {t.lower() for t in get_catalog().source(source_id).allowed_tables}
    if not allowed:
        return
    used = {t.lower() for t in _FROM_RE.findall(sql)}
    bad = sorted(used - allowed)
    if bad:
        raise QueryError(f"tables {bad} are not on the allow-list for {source_id}: {sorted(allowed)}")


async def describe_tables(source_id: str, tables: list[str]) -> dict:
    """Columns and types of source tables (exploration only).

    Args:
        source_id: a source from the semantic layer.
        tables: table names on that source's allow-list.
    """
    try:
        for t in tables:
            _check_tables(source_id, f"FROM {t}")
        conn = connectors.get_connector(get_catalog(), source_id)
        return {"status": "ok", "exploratory": True, "tables": await conn.describe(tables)}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


async def run_exploratory_sql(source_id: str, sql: str, tool_context: ToolContext = None) -> dict:
    """Run ONE read-only SELECT against a source for investigation. Results are
    EXPLORATORY: never an official figure, never a variance input.

    Args:
        source_id: a source from the semantic layer.
        sql: a single SELECT/WITH statement over allow-listed tables (max 200 rows returned).
    """
    try:
        _check_tables(source_id, sql)
        conn = connectors.get_connector(get_catalog(), source_id)
        res = await conn.run_readonly_sql(sql, max_rows=200, purpose="exploratory")
    except DataSourceUnavailableError:
        return {"status": "unavailable", "reason": f"{source_id} did not respond; try again shortly."}
    except Exception as e:
        return {"status": "error", "reason": str(e)}
    ev_id = new_id()
    if tool_context is not None:
        tool_context.state[f"evidence:{ev_id}"] = Evidence(
            evidence_id=ev_id, kind="exploratory", source_id=source_id, executed_sql=res.executed_sql,
            row_count=len(res.rows), exploratory=True).model_dump(mode="json")
    return {"status": "ok", "exploratory": True, "evidence_id": ev_id, "columns": res.columns,
            "rows": res.rows, "truncated": res.truncated, "executed_sql": res.executed_sql,
            "note": "EXPLORATORY result - not a governed metric."}
