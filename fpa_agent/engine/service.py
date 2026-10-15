"""
MetricService — the single, generic, deterministic path from a question's
structured intent to a grounded answer payload:

    semantic layer row -> governance gate -> period resolution -> QuerySpec
    -> connector (one direct hop, no nested agents) -> Decimal math
    -> netting proof -> evidence ledger -> JSON payload for the agent

It replaces Kaplan's uc1/uc2/uc3/uc4 dedicated tools: those existed to collapse
fetch+compute into one call for latency; this does the same for ANY metric or
analysis row, so a new client needs catalog rows, not new tools.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from fpa_agent.connectors import registry as connectors
from fpa_agent.engine import methods as M
from fpa_agent.engine.formulas import FormulaError, evaluate
from fpa_agent.engine.results import (
    D, Driver, VarianceResult, favorability, pct, quantize, round_to_total,
)
from fpa_agent.grounding.evidence import Evidence, EvidenceLedger, definition_hash
from fpa_agent.periods import FiscalCalendar, Period
from fpa_agent.query.compiler import PERIOD_ALIAS, QuerySpec
from fpa_agent.semantic.models import Analysis, Filter, Metric
from fpa_agent.semantic.repository import Catalog, get_catalog


class GateError(ValueError):
    """A governance gate refused the request (definition not confirmed, no
    source, analysis not live). The message is user-safe."""

    def __init__(self, message: str, status: str = "", owner: str = ""):
        super().__init__(message)
        self.status, self.owner = status, owner


class QueryError(ValueError):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Fetch results
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MetricFetch:
    metric: Metric
    period: Period
    group_by: list[str]
    time_grain: str | None
    groups: dict[tuple, Decimal | None]
    evidence: Evidence
    sql: list[str] = field(default_factory=list)

    @property
    def total(self) -> Decimal | None:
        return self.groups.get(())


def _dec(v) -> Decimal | None:
    if v is None or v == "":
        return None
    return D(v)


def _norm_period_key(v) -> int:
    if isinstance(v, (int,)):
        return v
    s = "".join(ch for ch in str(v) if ch.isdigit())
    if len(s) >= 6:
        return int(s[:6])
    return int(Decimal(str(v)))


def _fmt(v: Decimal | None, decimals: int) -> str | None:
    return quantize(v, decimals)


def _md_table(headers: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join("" if c is None else str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def _signed(s: str | None) -> str | None:
    if s is None:
        return None
    return s if s.startswith("-") else f"+{s}"


# ─────────────────────────────────────────────────────────────────────────────
# Analysis context (what methods see)
# ─────────────────────────────────────────────────────────────────────────────

class AnalysisContext:
    def __init__(self, service: "MetricService", inputs: dict[str, str], period: Period,
                 comparison_kind: str, comparison_period: Period, filters: dict[str, list],
                 dimension: str | None, params: dict):
        self.service = service
        self.inputs = inputs
        self.period = period
        self.comparison_kind = comparison_kind
        self.comparison_period = comparison_period
        self.filters = filters
        self.dimension = dimension
        self.params = params or {}
        self.labels = self.params.get("labels", {})
        self.fetches: list[MetricFetch] = []

    def what(self, role: str, side: str) -> str:
        p = self.period if side == "current" else self.comparison_period
        return f"{self.inputs.get(role, role)} ({p.label})"

    def require_dimension(self) -> str:
        if not self.dimension:
            raise M.MethodError("this method needs a dimension (analysis.dimension or breakdown)")
        return self.dimension

    def month_before(self, p: Period) -> Period:
        y, m = (p.start.year, p.start.month - 1) if p.start.month > 1 else (p.start.year - 1, 12)
        return Period(date(y, m, 1), p.start, f"{date(y, m, 1):%b %Y}", "month")

    def metric_for(self, role: str, side: str) -> str:
        if role not in self.inputs:
            raise M.MethodError(f"analysis has no metric bound to role {role!r}")
        if side == "comparison" and self.comparison_kind == "budget":
            mid = self.inputs.get(f"budget_{role}") or \
                self.service.catalog.metric(self.inputs[role]).budget_metric_id
            if not mid:
                raise M.MethodError(f"no budget metric for role {role!r} "
                                    f"(set inputs.budget_{role} or metric.budget_metric_id)")
            return mid
        return self.inputs[role]

    async def fetch(self, role: str, period: Period | None = None, group_by=None,
                    side: str = "current") -> MetricFetch:
        f = await self.service.fetch(self.metric_for(role, side),
                                     period or (self.period if side == "current" else self.comparison_period),
                                     self.filters, group_by or [])
        self.fetches.append(f)
        return f

    async def pair(self, role: str, group_by=None) -> tuple[MetricFetch, MetricFetch]:
        return await asyncio.gather(self.fetch(role, None, group_by, "current"),
                                    self.fetch(role, None, group_by, "comparison"))


# ─────────────────────────────────────────────────────────────────────────────
# Service
# ─────────────────────────────────────────────────────────────────────────────

class MetricService:
    def __init__(self, catalog: Catalog | None = None, ledger: EvidenceLedger | None = None,
                 as_of: date | None = None):
        self._catalog = catalog
        self.ledger = ledger or EvidenceLedger()
        self.as_of = as_of

    @property
    def catalog(self) -> Catalog:
        return self._catalog or get_catalog()

    @property
    def calendar(self) -> FiscalCalendar:
        return FiscalCalendar(self.catalog.profile.calendar, self.as_of)

    # ── governance gates (ported from Kaplan's definition-status gate) ──────
    def gate_metric(self, metric: Metric, _seen: tuple = ()) -> list[str]:
        """Raise GateError if the metric can't be computed; return caveats."""
        if metric.definition_status == "blocked":
            raise GateError(
                f"'{metric.term}' is a known metric but has no connected data source yet. "
                f"{metric.open_questions} Contact {metric.owner or 'the data team'} for this figure.",
                "blocked", metric.owner)
        if metric.definition_status == "incomplete":
            raise GateError(
                f"'{metric.term}' is a known metric, but its exact definition or source has not "
                f"been confirmed yet. {metric.open_questions or 'Confirmation is pending.'} "
                f"Owner: {metric.owner or 'unknown'}.", "incomplete", metric.owner)
        if metric.category == "causal":
            raise GateError(
                f"'{metric.term}' is a qualitative driver, not a figure the systems report; "
                f"it can be discussed but not computed.", "causal", metric.owner)
        caveats = []
        if metric.definition_status == "partial":
            caveats.append(f"'{metric.term}' definition is provisional: "
                           f"{metric.open_questions or 'pending final sign-off'} (owner: {metric.owner}).")
        for dep in metric.depends_on:
            if dep in _seen:
                raise GateError(f"circular formula involving {dep!r}", "invalid")
            caveats += self.gate_metric(self.catalog.metric(dep), _seen + (metric.metric_id,))
        return caveats

    def resolve_period(self, spec: dict | Period) -> Period:
        return spec if isinstance(spec, Period) else self.calendar.resolve(spec)

    # ── fetching ────────────────────────────────────────────────────────────
    async def fetch(self, metric_id: str, period: Period, filters: dict[str, list] | None = None,
                    group_by: list[str] | None = None, time_grain: str | None = None,
                    _stack: tuple = ()) -> MetricFetch:
        metric = self.catalog.metric(metric_id)
        filters, group_by = dict(filters or {}), list(group_by or [])
        if metric.metric_type == "derived":
            return await self._fetch_derived(metric, period, filters, group_by, time_grain, _stack)
        return await self._fetch_base(metric, period, filters, group_by, time_grain)

    def build_spec(self, metric: Metric, period: Period, filters: dict[str, list],
                   group_by: list[str], time_grain: str | None) -> QuerySpec:
        source = self.catalog.source(metric.source_id or "")
        extra: list[Filter] = []
        for dim, values in filters.items():
            col = metric.dimensions.get(dim)
            if not col:
                raise QueryError(f"'{metric.term}' cannot be filtered by {dim!r}; "
                                 f"available dimensions: {sorted(metric.dimensions)}")
            vals = values if isinstance(values, list) else [values]
            extra.append(Filter(column=col, operator="IN", values=vals))
        gb = {}
        for dim in group_by:
            col = metric.dimensions.get(dim)
            if not col:
                raise QueryError(f"'{metric.term}' cannot be broken down by {dim!r}; "
                                 f"available dimensions: {sorted(metric.dimensions)}")
            gb[dim] = col
        fiscal_years: list[int] = []
        if metric.time.pattern == "fiscal_year":
            cal = self.calendar
            first, last = cal.fiscal_year_of(period.start), cal.fiscal_year_of(period.end_inclusive)
            if cal.fiscal_year_start(first) != period.start or cal.fiscal_year_start(last + 1) != period.end:
                raise QueryError(f"'{metric.term}' is only reported for whole fiscal years; "
                                 f"{period.label} is not a whole fiscal year")
            fiscal_years = list(range(first, last + 1))
        if metric.time.pattern != "date" and not period.month_aligned:
            raise QueryError(f"'{metric.term}' is reported monthly; use whole months")
        return QuerySpec(
            dialect=source.dialect, table=metric.table, table_alias=metric.table_alias,
            joins=metric.joins, measures={metric.metric_id: metric.measure},
            filters=list(metric.filters) + extra, time=metric.time,
            period_start=period.start, period_end=period.end, fiscal_years=fiscal_years,
            snapshot=metric.time_semantics == "snapshot", group_by=gb, time_grain=time_grain,
        )

    async def _fetch_base(self, metric: Metric, period: Period, filters, group_by, time_grain) -> MetricFetch:
        if not metric.measure or not metric.table or not metric.source_id:
            raise GateError(f"'{metric.term}' has no source mapping yet.", "incomplete", metric.owner)
        spec = self.build_spec(metric, period, filters, group_by, time_grain)
        conn = connectors.get_connector(self.catalog, metric.source_id)
        res = await conn.run_spec(spec)
        key_cols = ([PERIOD_ALIAS] if time_grain else []) + group_by
        groups: dict[tuple, Decimal | None] = {}
        for row in res.rows:
            key = tuple(
                _norm_period_key(row.get(c)) if c == PERIOD_ALIAS else
                (None if row.get(c) is None else str(row.get(c)))
                for c in key_cols)
            val = _dec(row.get(metric.metric_id))
            groups[key] = val if key not in groups else (groups[key] or Decimal(0)) + (val or Decimal(0))
        if not key_cols and not groups:
            groups[()] = None
        ev = self.ledger.add(
            kind="metric_query", source_id=metric.source_id, metric_id=metric.metric_id,
            definition_hash=definition_hash(metric), definition_status=metric.definition_status,
            period=period.to_dict(), filters=filters, group_by=key_cols,
            executed_sql=res.executed_sql, row_count=len(res.rows),
            values=_values_for_evidence(groups, metric.decimals),
        )
        return MetricFetch(metric, period, group_by, time_grain, groups, ev, [res.executed_sql])

    async def _fetch_derived(self, metric: Metric, period, filters, group_by, time_grain, stack) -> MetricFetch:
        if metric.metric_id in stack:
            raise GateError(f"circular formula involving {metric.metric_id!r}", "invalid")
        deps = metric.depends_on
        fetched = await asyncio.gather(*[
            self.fetch(d, period, filters, group_by, time_grain, stack + (metric.metric_id,)) for d in deps])
        by_dep = dict(zip(deps, fetched))
        keys = list(dict.fromkeys(k for f in fetched for k in f.groups))
        groups: dict[tuple, Decimal | None] = {}
        notes = []
        for k in keys:
            try:
                groups[k] = evaluate(metric.formula, {d: by_dep[d].groups.get(k) for d in deps})
            except FormulaError as e:
                groups[k] = None
                notes.append(f"{k or 'total'}: {e}")
        ev = self.ledger.add(
            kind="derived", metric_id=metric.metric_id, definition_hash=definition_hash(metric),
            definition_status=metric.definition_status, period=period.to_dict(), filters=filters,
            group_by=([PERIOD_ALIAS] if time_grain else []) + group_by, formula=metric.formula,
            inputs=[f.evidence.evidence_id for f in fetched],
            values=_values_for_evidence(groups, metric.decimals) | ({"notes": "; ".join(notes)} if notes else {}),
        )
        return MetricFetch(metric, period, group_by, time_grain, groups, ev,
                           [s for f in fetched for s in f.sql])

    # ── public API (what the agent tools call) ──────────────────────────────
    async def get_metric(self, metric_id: str, period: dict | Period, filters: dict | None = None,
                         group_by: list[str] | None = None, time_grain: str | None = None) -> dict:
        metric = self.catalog.metric(metric_id)
        caveats = self.gate_metric(metric)
        p = self.resolve_period(period)
        caveats += self._period_caveats(p)
        f = await self.fetch(metric_id, p, filters, group_by, time_grain)
        out = {
            "metric_id": metric.metric_id, "term": metric.term, "unit": metric.unit,
            "currency": self.catalog.profile.currency if metric.unit == "currency" else None,
            "definition": metric.definition, "period": p.to_dict(), "filters": filters or {},
            "evidence_id": f.evidence.evidence_id, "executed_sql": f.sql, "caveats": caveats,
        }
        if not group_by and not time_grain:
            out["value"] = _fmt(f.total, metric.decimals)
            if f.total is None:
                out["no_data"] = True
        else:
            cols = (["period"] if time_grain else []) + list(group_by or [])
            rows = []
            for k, v in sorted(f.groups.items(), key=lambda kv: tuple("" if x is None else str(x) for x in kv[0])):
                rows.append({**dict(zip(cols, k)), "value": _fmt(v, metric.decimals)})
            out["rows"] = rows
            total = await self.fetch(metric_id, p, filters) if group_by else None
            if total is not None:
                out["total"] = _fmt(total.total, metric.decimals)
                if not metric.additive:
                    out["note"] = ("This metric is not additive across categories; category values "
                                   "may not sum to the total. Quote the total for the overall figure.")
            out["table"] = _md_table(cols + [metric.term], [[r.get(c) for c in cols] + [r["value"]] for r in rows])
        return out

    async def compare_metric(self, metric_id: str, period: dict | Period, comparison: str | None = None,
                             filters: dict | None = None, breakdown: str | None = None) -> dict:
        metric = self.catalog.metric(metric_id)
        ad_hoc = Analysis(
            analysis_id=f"adhoc_{metric_id}", title=f"{metric.term} variance",
            headline_metric=metric_id, method="contribution" if breakdown else "delta",
            inputs={"value": metric_id}, dimension=breakdown, status="live",
        )
        return await self._run(ad_hoc, period, comparison, filters, None)

    async def run_analysis(self, analysis_id: str, period: dict | Period, comparison: str | None = None,
                           filters: dict | None = None, breakdown: str | None = None) -> dict:
        analysis = self.catalog.analysis(analysis_id)
        if analysis.status == "roadmap":
            raise GateError(f"'{analysis.title}' is on the roadmap and not available yet.", "roadmap",
                            analysis.owner)
        return await self._run(analysis, period, comparison, filters, breakdown)

    async def list_dimension_values(self, metric_id: str, dimension_id: str,
                                    period: dict | Period | None = None) -> dict:
        metric = self.catalog.metric(metric_id)
        self.gate_metric(metric)
        if metric.metric_type == "derived":
            metric = self.catalog.metric(metric.depends_on[0])
        p = self.resolve_period(period) if period else None
        col = metric.dimensions.get(dimension_id)
        if not col:
            raise QueryError(f"{metric.term} has no dimension {dimension_id!r}")
        spec = self.build_spec(metric, p, {}, [], None) if p else QuerySpec(
            dialect=self.catalog.source(metric.source_id).dialect, table=metric.table,
            table_alias=metric.table_alias, joins=metric.joins, measures={}, filters=metric.filters)
        spec = spec.model_copy(update={"distinct_values_of": col, "measures": {}, "group_by": {},
                                       "snapshot": False, "limit": 500})
        res = await connectors.get_connector(self.catalog, metric.source_id).run_spec(spec)
        values = [r["value"] for r in res.rows if r.get("value") is not None]
        ev = self.ledger.add(kind="dimension_values", source_id=metric.source_id, metric_id=metric.metric_id,
                             executed_sql=res.executed_sql, row_count=len(values),
                             values={"count": len(values)})
        return {"metric_id": metric.metric_id, "dimension": dimension_id, "values": values,
                "evidence_id": ev.evidence_id, "executed_sql": [res.executed_sql]}

    # ── analysis execution ──────────────────────────────────────────────────
    def _period_caveats(self, p: Period) -> list[str]:
        if not self.calendar.is_closed(p):
            y, m = self.calendar.last_closed_month()
            return [f"{p.label} includes a month that is not closed yet (last closed month: "
                    f"{date(y, m, 1):%b %Y}); figures may still change."]
        return []

    async def _run(self, analysis: Analysis, period, comparison, filters, breakdown) -> dict:
        spec = M.get_method(analysis.method)
        cat = self.catalog
        headline = cat.metric(analysis.headline_metric)
        comparison = comparison or analysis.default_comparison or cat.profile.calendar.default_comparison
        if comparison not in spec.comparisons:
            raise M.MethodError(f"{analysis.method} does not support comparison {comparison!r} "
                                f"(supported: {list(spec.comparisons)})")
        missing = [r for r in spec.required_roles if r not in analysis.inputs]
        if missing:
            raise M.MethodError(f"analysis {analysis.analysis_id} is missing role bindings {missing}")

        caveats: list[str] = []
        for mid in dict.fromkeys([analysis.headline_metric, *analysis.inputs.values()]):
            caveats += self.gate_metric(cat.metric(mid))
        caveats = list(dict.fromkeys(caveats))
        p = self.resolve_period(period)
        cp = self.calendar.comparison(p, comparison)
        if comparison == "budget":
            cp = Period(cp.start, cp.end, f"Budget {p.label}", cp.grain, cp.fiscal)
        caveats += self._period_caveats(p)

        ctx = AnalysisContext(self, analysis.inputs, p, comparison, cp, filters or {},
                              breakdown or analysis.dimension, analysis.params)
        result: VarianceResult = await spec.run(ctx)

        # Post-netting adjustments (e.g. Kaplan's "timing" line): fetched live,
        # shown after the proof, never part of it.
        for adj in analysis.params.get("adjustments", []):
            f = await self.fetch(adj["metric_id"], p, filters or {})
            ctx.fetches.append(f)
            if f.total is not None:
                result.post_netting.append(Driver(label=adj.get("label", adj["metric_id"]), role="adjustment",
                                                  contribution=D(adj.get("sign", -1)) * f.total))
        return self._render(analysis, headline, result, p, cp, comparison, ctx, caveats)

    def _render(self, analysis, headline: Metric, r: VarianceResult, p: Period, cp: Period,
                comparison: str, ctx: AnalysisContext, caveats: list[str]) -> dict:
        dec = headline.decimals
        if analysis.method == "ratio_bridge":
            dec = max(dec, 2)
        display = round_to_total({d.label: d.contribution for d in r.drivers}, r.headline, dec)
        change_disp = quantize(r.headline, dec)
        drivers_out = []
        for d in r.drivers:
            share = pct(d.contribution, r.headline) if r.headline else None
            drivers_out.append({"driver": d.label, "role": d.role,
                                "contribution": _signed(quantize(display[d.label], dec)),
                                "share_of_change_pct": quantize(share, 1) if share is not None else None})
        rows = [[x["driver"], x["contribution"], (x["share_of_change_pct"] + "%") if x["share_of_change_pct"] else ""]
                for x in drivers_out]
        post = [{"driver": d.label, "contribution": _signed(quantize(d.contribution, dec))} for d in r.post_netting]
        rows += [[x["driver"] + " (adjustment)", x["contribution"], ""] for x in post]
        adjusted = r.headline + sum((d.contribution for d in r.post_netting), Decimal(0))

        fetch_ids = list(dict.fromkeys(f.evidence.evidence_id for f in ctx.fetches))
        sqls = list(dict.fromkeys(s for f in ctx.fetches for s in f.sql))
        metrics_used = list(dict.fromkeys(f.metric.metric_id for f in ctx.fetches))
        out: dict[str, Any] = {
            "analysis_id": analysis.analysis_id, "title": analysis.title, "method": analysis.method,
            "headline_metric": {"metric_id": headline.metric_id, "term": headline.term, "unit": headline.unit,
                                "favorable_direction": headline.favorable_direction},
            "currency": self.catalog.profile.currency if headline.unit == "currency" else None,
            "period": p.to_dict(),
            "comparison": {"kind": comparison, "period": cp.to_dict()},
            "dimension": ctx.dimension,
            "current_value": quantize(r.current_value, dec),
            "comparison_value": quantize(r.comparison_value, dec),
            "change": _signed(change_disp),
            "change_pct": quantize(pct(r.headline, r.comparison_value), 2) if r.comparison_value else None,
            "favorability": favorability(r.headline, headline.favorable_direction),
            "drivers": drivers_out,
            "drivers_table": _md_table(["Driver", "Contribution", "Share of change"], rows),
            "nets_to_headline": r.nets_to_headline,
            "netting_proof": "Σ drivers = change, verified exactly in Decimal before display rounding.",
            "post_netting_adjustments": post,
            "adjusted_change": _signed(quantize(adjusted, dec)) if post else None,
            "details": {k: quantize(D(v), 4 if abs(D(v)) < 10 else dec) if isinstance(v, (Decimal, int, float)) else v
                        for k, v in r.presentation.items()},
            "definitions": [
                {"metric_id": m.metric_id, "term": m.term, "definition": m.definition,
                 "status": m.definition_status, "owner": m.owner}
                for m in (self.catalog.metric(x) for x in metrics_used)],
            "caveats": caveats,
            "executed_sql": sqls,
        }
        ev = self.ledger.add(
            kind="analysis", analysis_id=analysis.analysis_id, metric_id=headline.metric_id,
            definition_hash=definition_hash(analysis), period=p.to_dict(), filters=ctx.filters,
            inputs=fetch_ids,
            values={"current_value": out["current_value"], "comparison_value": out["comparison_value"],
                    "change": out["change"], "change_pct": out["change_pct"],
                    **{f"driver:{d['driver']}": d["contribution"] for d in drivers_out}},
        )
        out["evidence_id"] = ev.evidence_id
        out["input_evidence_ids"] = fetch_ids
        return out


def _values_for_evidence(groups: dict[tuple, Decimal | None], decimals: int, limit: int = 200) -> dict:
    out = {}
    for i, (k, v) in enumerate(groups.items()):
        if i >= limit:
            out["truncated"] = True
            break
        out["|".join("" if x is None else str(x) for x in k) or "total"] = quantize(v, max(decimals, 2)) if v is not None else None
    return out
