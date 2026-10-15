"""
Semantic-layer lint — run before every catalog change goes live (CI, the
admin loader, and `fpa validate`). The same checks the engine relies on at
runtime, surfaced up front with row-level messages:

  static: references resolve, expressions are safe, time bindings complete,
          methods/roles/comparisons consistent, formulas parse and are acyclic,
          reference SQL is read-only, tables on the source allow-list;
  live (optional): every referenced column exists in the source schema.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from fpa_agent.engine.formulas import FormulaError, parse
from fpa_agent.engine.methods import METHODS
from fpa_agent.periods import FiscalCalendar
from fpa_agent.query.compiler import (
    QueryCompileError, assert_read_only, validate_expression, validate_identifier,
)
from fpa_agent.semantic.repository import Catalog

_IDENT_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?")


@dataclass
class Issue:
    level: str        # error | warning
    table: str
    row_id: str
    message: str

    def __str__(self) -> str:
        return f"[{self.level.upper()}] {self.table}:{self.row_id} — {self.message}"


def validate_catalog(cat: Catalog) -> list[Issue]:
    issues: list[Issue] = []

    def err(t, i, m):
        issues.append(Issue("error", t, i, m))

    def warn(t, i, m):
        issues.append(Issue("warning", t, i, m))

    # ── sources ─────────────────────────────────────────────────────────────
    for s in cat.sources.values():
        mode = s.connection.get("mode")
        if mode not in ("local", "mcp"):
            err("sources", s.source_id, f"connection.mode must be local or mcp, got {mode!r}")
        for t in s.allowed_tables:
            try:
                validate_identifier(t, "allowed table")
            except QueryCompileError as e:
                err("sources", s.source_id, str(e))
        for k, v in s.connection.items():
            if any(w in k.lower() for w in ("password", "secret", "token")) and not k.endswith("_env"):
                err("sources", s.source_id, f"connection.{k} looks like a secret; store only env var names")

    # ── metrics ─────────────────────────────────────────────────────────────
    for m in cat.metrics.values():
        mid = m.metric_id
        if m.metric_type == "derived":
            try:
                parse(m.formula or "")
            except FormulaError as e:
                err("metrics", mid, str(e))
            gated = m.definition_status in ("incomplete", "blocked")
            for dep in m.depends_on:
                if dep not in cat.metrics:
                    # an un-confirmed metric may point at a row that doesn't exist yet
                    (warn if gated else err)("metrics", mid, f"formula references unknown metric {dep!r}")
            if _has_cycle(cat, mid):
                err("metrics", mid, "formula dependencies are circular")
        elif m.category == "identity" and m.definition_status in ("confirmed", "partial"):
            if not m.source_id or m.source_id not in cat.sources:
                err("metrics", mid, f"source_id {m.source_id!r} is not a known source")
            if not m.table or not m.measure:
                err("metrics", mid, "confirmed/partial base metric needs table and measure")
            for what, expr in [("table", m.table), ("table_alias", m.table_alias)]:
                if expr:
                    try:
                        validate_identifier(expr, what)
                    except QueryCompileError as e:
                        err("metrics", mid, str(e))
            for what, expr in [("measure", m.measure),
                               *[(f"filter {f.column}", f.column) for f in m.filters],
                               *[(f"dimension {d}", c) for d, c in m.dimensions.items()]]:
                if expr:
                    try:
                        validate_expression(expr, what)
                    except QueryCompileError as e:
                        err("metrics", mid, str(e))
            if m.source_id in cat.sources and m.table:
                allowed = {t.lower() for t in cat.sources[m.source_id].allowed_tables}
                used = [m.table] + [j.table for j in m.joins]
                if allowed and any(t.lower() not in allowed for t in used):
                    err("metrics", mid, f"table(s) {used} not on the {m.source_id} allow-list")
            t = m.time
            need = {"date": ["column"], "period_key": ["column"], "fiscal_year": ["column"],
                    "year_month": ["year_column", "month_column"], "none": []}[t.pattern]
            for f in need:
                if not getattr(t, f):
                    err("metrics", mid, f"time.pattern={t.pattern} needs time.{f}")
            if t.pattern == "none" and m.time_semantics == "flow":
                warn("metrics", mid, "no time binding: the value ignores the requested period")
        for d in m.dimensions:
            if d not in cat.dimensions:
                err("metrics", mid, f"dimension {d!r} is not defined in sl_dimensions")
        if m.budget_metric_id and m.budget_metric_id not in cat.metrics:
            err("metrics", mid, f"budget_metric_id {m.budget_metric_id!r} is unknown")
        if m.reference_sql:
            try:
                assert_read_only(m.reference_sql)
            except QueryCompileError as e:
                err("metrics", mid, f"reference_sql: {e}")
        elif m.definition_status == "confirmed" and m.category == "identity":
            warn("metrics", mid, "confirmed metric has no reference_sql — it cannot be auto-reconciled")
        if m.definition_status in ("partial", "incomplete", "blocked") and not m.open_questions:
            warn("metrics", mid, f"status {m.definition_status} should explain open_questions")
        if not m.owner:
            warn("metrics", mid, "no owner")

    # aliases must not be ambiguous across metrics
    seen: dict[str, str] = {}
    for m in cat.metrics.values():
        for a in [m.term, *m.aliases]:
            k = a.strip().lower()
            if k in seen and seen[k] != m.metric_id:
                warn("metrics", m.metric_id, f"alias {a!r} also used by {seen[k]!r} (ambiguous)")
            seen.setdefault(k, m.metric_id)

    # ── analyses ────────────────────────────────────────────────────────────
    for a in cat.analyses.values():
        aid = a.analysis_id
        if a.headline_metric not in cat.metrics:
            err("analyses", aid, f"headline_metric {a.headline_metric!r} is unknown")
        spec = METHODS.get(a.method)
        if spec is None:
            err("analyses", aid, f"method {a.method!r} is not registered ({sorted(METHODS)})")
            continue
        for r in spec.required_roles:
            if r not in a.inputs:
                err("analyses", aid, f"method {a.method} needs inputs.{r}")
        for role, mid in a.inputs.items():
            if mid not in cat.metrics:
                err("analyses", aid, f"inputs.{role} -> unknown metric {mid!r}")
        if spec.needs_dimension:
            if not a.dimension:
                err("analyses", aid, f"method {a.method} needs a dimension")
            else:
                for role in spec.required_roles:
                    mid = a.inputs.get(role)
                    if mid in cat.metrics and a.dimension not in cat.dimensions_of(mid):
                        err("analyses", aid, f"metric {mid} (role {role}) has no dimension {a.dimension!r}")
        if a.default_comparison and a.default_comparison not in spec.comparisons:
            err("analyses", aid, f"{a.method} does not support comparison {a.default_comparison}")
        if a.default_comparison == "budget":
            for role in spec.required_roles:
                mid = a.inputs.get(role)
                if mid in cat.metrics and not (a.inputs.get(f"budget_{role}") or cat.metrics[mid].budget_metric_id):
                    err("analyses", aid, f"budget comparison but no budget metric for role {role}")
        if a.method == "rollforward_bridge":
            fd = a.params.get("flow_dimension")
            flows = a.inputs.get("flows")
            if not fd:
                err("analyses", aid, "rollforward_bridge needs params.flow_dimension")
            elif flows in cat.metrics and fd not in cat.dimensions_of(flows):
                err("analyses", aid, f"flows metric {flows} has no dimension {fd!r}")
            lvl = a.inputs.get("level")
            if lvl in cat.metrics and cat.metrics[lvl].time_semantics != "snapshot":
                warn("analyses", aid, f"level metric {lvl} should have time_semantics: snapshot")
        for adj in a.params.get("adjustments", []):
            if adj.get("metric_id") not in cat.metrics:
                err("analyses", aid, f"adjustment metric {adj.get('metric_id')!r} is unknown")
        if a.status == "live" and not a.question_examples:
            warn("analyses", aid, "no question_examples — add a few to help routing")

    # ── glossary / profiles / tests ─────────────────────────────────────────
    for g in cat.glossary.values():
        for mid in g.related_metrics:
            if mid not in cat.metrics:
                warn("glossary", g.term, f"related metric {mid!r} is unknown")
    if "fpa_analyst" not in cat.agent_profiles:
        warn("agent_profiles", "fpa_analyst", "no persona defined — a generic one will be used")
    cal = FiscalCalendar(cat.profile.calendar)
    for t in cat.recon_tests.values():
        if t.metric_id and t.metric_id not in cat.metrics:
            err("reconciliation_tests", t.test_id, f"unknown metric {t.metric_id!r}")
        if t.analysis_id and t.analysis_id not in cat.analyses:
            err("reconciliation_tests", t.test_id, f"unknown analysis {t.analysis_id!r}")
        if t.kind in ("metric", "analysis") and not (t.metric_id or t.analysis_id):
            err("reconciliation_tests", t.test_id, "needs metric_id or analysis_id")
        try:
            cal.resolve(t.period)
        except (ValueError, KeyError) as e:
            err("reconciliation_tests", t.test_id, f"period: {e}")
        if t.reference_sql:
            try:
                assert_read_only(t.reference_sql)
            except QueryCompileError as e:
                err("reconciliation_tests", t.test_id, f"reference_sql: {e}")
    return issues


def _has_cycle(cat: Catalog, start: str) -> bool:
    stack, seen = [(start, ())], set()
    while stack:
        node, path = stack.pop()
        if node in path:
            return True
        m = cat.metrics.get(node)
        if m is None or (node, path) in seen:
            continue
        seen.add((node, path))
        stack.extend((d, path + (node,)) for d in m.depends_on)
    return False


async def validate_against_sources(cat: Catalog) -> list[Issue]:
    """Live check: every column a confirmed/partial base metric references
    exists in the source table (catches typos and schema drift)."""
    from fpa_agent.connectors import registry
    issues: list[Issue] = []
    by_source: dict[str, set[str]] = {}
    for m in cat.metrics.values():
        if m.metric_type == "base" and m.is_queryable and m.table and m.source_id:
            by_source.setdefault(m.source_id, set()).add(m.table)
    schemas: dict[str, dict[str, set[str]]] = {}
    for sid, tables in by_source.items():
        try:
            desc = await registry.get_connector(cat, sid).describe(sorted(tables))
            schemas[sid] = {t: {c["column"].lower() for c in cols if c.get("column")} for t, cols in desc.items()}
        except Exception as e:
            issues.append(Issue("error", "sources", sid, f"cannot describe tables: {e}"))
    sql_words = {"sum", "count", "avg", "min", "max", "distinct", "case", "when", "then", "else", "end",
                 "and", "or", "not", "in", "is", "null", "coalesce", "nullif", "cast", "as", "round",
                 "abs", "integer", "real", "float", "like", "between", "extract", "from", "year", "month"}
    for m in cat.metrics.values():
        if m.metric_type != "base" or not m.is_queryable or m.source_id not in schemas:
            continue
        cols = schemas[m.source_id].get(m.table, set())
        if not cols:
            issues.append(Issue("error", "metrics", m.metric_id, f"table {m.table} not found or has no columns"))
            continue
        exprs = [m.measure or "", *[f.column for f in m.filters], *m.dimensions.values(),
                 m.time.column or "", m.time.year_column or "", m.time.month_column or ""]
        literals = re.compile(r"'(?:[^']|'')*'")
        for e in exprs:
            for tok in _IDENT_TOKEN.findall(literals.sub("", e)):
                name = tok.split(".")[-1].lower()
                if name in sql_words or (m.joins and "." in tok):
                    continue
                if name not in cols:
                    issues.append(Issue("error", "metrics", m.metric_id,
                                        f"column {tok!r} not found in {m.source_id}.{m.table}"))
    return issues
