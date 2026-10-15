"""
Source reconciliation — "does the agent's number match what the client's own
system says?", as a repeatable integration test instead of a one-off manual
check (the 13/13 UAT reconciliation in the Kaplan build, automated).

For every ReconTest (sl_reconciliation_tests, or auto-generated):

  agent side      the EXACT code path the agent's tools use (MetricService):
                  semantic layer -> compiled SQL -> source -> Decimal math
  reference side  an INDEPENDENT figure: the client's own SQL run read-only on
                  the source (test.reference_sql, else metric.reference_sql),
                  or a literal from the client's report (expected_value)
  verdict         |agent - reference| <= max(tolerance_abs, tolerance_pct·|ref|)
                  per group for breakdowns; analyses must also net exactly.

Results go to the console and to JSON / Markdown / JUnit XML so the run can
gate a deployment in CI and be handed to the client as sign-off evidence.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from fpa_agent.connectors import registry as connectors
from fpa_agent.engine.results import D
from fpa_agent.engine.service import MetricService
from fpa_agent.periods import FiscalCalendar, Period
from fpa_agent.semantic.models import ReconTest
from fpa_agent.semantic.repository import Catalog


@dataclass
class ReconResult:
    test_id: str
    kind: str
    status: str                      # pass | fail | error | skipped
    description: str = ""
    period: str = ""
    agent_value: str | None = None
    reference_value: str | None = None
    difference: str | None = None
    tolerance: str | None = None
    reference_kind: str | None = None   # sql | expected_value
    mismatches: list[dict] = field(default_factory=list)
    agent_sql: list[str] = field(default_factory=list)
    reference_sql: str | None = None
    evidence_id: str | None = None
    message: str = ""
    duration_ms: int = 0


def period_params(cal: FiscalCalendar, p: Period, prefix: str = "") -> dict[str, Any]:
    out = {
        f"{prefix}start_date": p.start.isoformat(),
        f"{prefix}end_date": p.end.isoformat(),
        f"{prefix}last_date": p.end_inclusive.isoformat(),
        f"{prefix}start_yyyymm": p.start_yyyymm,
        f"{prefix}end_yyyymm": p.end_yyyymm,
        f"{prefix}start_ym": f"{p.start.year:04d}-{p.start.month:02d}",
        f"{prefix}end_ym": f"{p.end_inclusive.year:04d}-{p.end_inclusive.month:02d}",
        f"{prefix}year": p.start.year,
    }
    fy = cal.fiscal_year_of(p.start)
    if cal.fiscal_year_start(fy) == p.start and cal.fiscal_year_start(fy + 1) == p.end:
        out[f"{prefix}fiscal_year"] = fy
    return out


def _num(v) -> str | None:
    """Readable, still-precise rendering for reports (max 6 dp)."""
    if v is None:
        return None
    d = D(v)
    if d == d.to_integral_value():
        return str(d.quantize(Decimal(1)))
    return format(d.quantize(Decimal("0.000001")).normalize(), "f")


def _within(a: Decimal, r: Decimal, t: ReconTest) -> bool:
    tol = max(D(t.tolerance_abs), D(t.tolerance_pct) / 100 * abs(r))
    return abs(a - r) <= tol


def _scalar(rows: list[dict]) -> Decimal | None:
    if not rows:
        return None
    row = {k.lower(): v for k, v in rows[0].items()}
    v = row["value"] if "value" in row else next(iter(row.values()), None)
    return None if v is None else D(v)


class ReconciliationRunner:
    def __init__(self, catalog: Catalog, as_of: date | None = None, concurrency: int = 4):
        self.catalog = catalog
        self.as_of = as_of
        self.cal = FiscalCalendar(catalog.profile.calendar, as_of)
        self.sem = asyncio.Semaphore(concurrency)

    # ── selection ───────────────────────────────────────────────────────────
    def select(self, ids: list[str] | None = None, tags: list[str] | None = None,
               kinds: tuple[str, ...] = ("metric", "analysis")) -> list[ReconTest]:
        tests = [t for t in self.catalog.recon_tests.values() if t.kind in kinds]
        if ids:
            tests = [t for t in tests if t.test_id in ids]
        if tags:
            tests = [t for t in tests if set(tags) & set(t.tags)]
        return tests

    def auto_tests(self, months: int = 3, include_fiscal_year: bool = True) -> list[ReconTest]:
        """One test per confirmed metric that has catalog reference_sql, for
        each of the last N closed months (+ the last closed fiscal year)."""
        ly, lm = self.cal.last_closed_month()
        periods = []
        y, m = ly, lm
        for _ in range(months):
            periods.append(({"type": "month", "year": y, "month": m}, f"{y}{m:02d}"))
            y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        if include_fiscal_year:
            fy = self.cal.fiscal_year_of(date(ly, lm, 1))
            if self.cal.fiscal_year_start(fy + 1) > date(ly, lm, 1):
                fy -= 1
            periods.append(({"type": "year", "fiscal": True, "year": fy}, f"fy{fy}"))
        tests = []
        for metric in self.catalog.metrics.values():
            if not (metric.reference_sql and metric.is_queryable and metric.definition_status == "confirmed"):
                continue
            for spec, tag in periods:
                if metric.time.pattern == "fiscal_year" and spec["type"] != "year":
                    continue
                tests.append(ReconTest(
                    test_id=f"auto_{metric.metric_id}_{tag}", kind="metric", metric_id=metric.metric_id,
                    description=f"[auto] {metric.term} {tag}", period=spec,
                    tolerance_abs=0.01 if metric.unit in ("currency", "ratio") else 0, tags=["auto"]))
        return tests

    # ── execution ───────────────────────────────────────────────────────────
    async def run(self, tests: list[ReconTest]) -> list[ReconResult]:
        async def one(t):
            async with self.sem:
                return await self.run_one(t)
        return list(await asyncio.gather(*[one(t) for t in tests]))

    async def run_one(self, t: ReconTest) -> ReconResult:
        t0 = time.perf_counter()
        res = ReconResult(test_id=t.test_id, kind=t.kind, status="error", description=t.description)
        try:
            if t.kind == "metric":
                await self._metric(t, res)
            elif t.kind == "analysis":
                await self._analysis(t, res)
            else:
                res.status, res.message = "skipped", "question tests run with --e2e (agent_eval)"
        except Exception as e:  # report, never crash the run
            res.status, res.message = "error", f"{type(e).__name__}: {e}"
        res.duration_ms = int((time.perf_counter() - t0) * 1000)
        return res

    def _reference_sql(self, t: ReconTest) -> tuple[str | None, str | None]:
        # precedence: test SQL > test literal (expected_value) > metric's catalog SQL
        if t.reference_sql:
            return t.reference_sql, t.reference_source_id
        if t.expected_value is not None:
            return None, t.reference_source_id
        if t.kind == "metric" and t.metric_id and not t.group_by and not t.filters:
            m = self.catalog.metric(t.metric_id)
            return m.reference_sql, t.reference_source_id
        return None, t.reference_source_id

    def _source_for(self, metric_id: str) -> str:
        m = self.catalog.metric(metric_id)
        if m.metric_type == "base":
            return m.source_id
        return self._source_for(m.depends_on[0])

    async def _run_reference(self, sql: str, source_id: str, params: dict) -> tuple[list[dict], str]:
        conn = connectors.get_connector(self.catalog, source_id)
        r = await conn.run_reference_sql(sql, params)
        return r.rows, r.executed_sql

    async def _metric(self, t: ReconTest, res: ReconResult) -> None:
        svc = MetricService(self.catalog, as_of=self.as_of)
        metric = self.catalog.metric(t.metric_id)
        svc.gate_metric(metric)
        p = svc.resolve_period(t.period)
        res.period = p.label
        f = await svc.fetch(t.metric_id, p, t.filters, t.group_by)
        res.agent_sql, res.evidence_id = f.sql, f.evidence.evidence_id
        sql, src = self._reference_sql(t)
        src = src or self._source_for(t.metric_id)
        if sql:
            rows, res.reference_sql = await self._run_reference(sql, src, period_params(self.cal, p))
            res.reference_kind = "sql"
        elif t.expected_value is not None:
            rows, res.reference_kind = [{"value": t.expected_value}], "expected_value"
        else:
            res.status, res.message = "skipped", "no reference_sql or expected_value"
            return
        res.tolerance = str(max(t.tolerance_abs, 0)) + (f" or {t.tolerance_pct}%" if t.tolerance_pct else "")
        if t.group_by:
            ref = {tuple(str(r[c]) if r.get(c) is not None else None for c in t.group_by): r.get("value")
                   for r in rows}
            agent = f.groups
            for k in sorted(set(ref) | set(agent), key=lambda x: tuple("" if v is None else v for v in x)):
                a, r = agent.get(k), ref.get(k)
                if a is None and r is None:
                    continue
                if a is None or r is None or not _within(D(a), D(r), t):
                    res.mismatches.append({"group": dict(zip(t.group_by, k)), "agent": _num(a),
                                           "reference": _num(r)})
            res.agent_value = f"{len(agent)} groups"
            res.reference_value = f"{len(ref)} groups"
            res.status = "fail" if res.mismatches else "pass"
            res.message = f"{len(res.mismatches)} group(s) differ" if res.mismatches else "all groups match"
            return
        a, r = f.total, _scalar(rows)
        res.agent_value, res.reference_value = _num(a), _num(r)
        if a is None or r is None:
            res.status = "pass" if a is None and r is None else "fail"
            res.message = "one side returned no data" if res.status == "fail" else "both empty"
            return
        res.difference = _num(a - r)
        res.status = "pass" if _within(a, r, t) else "fail"

    async def _analysis(self, t: ReconTest, res: ReconResult) -> None:
        svc = MetricService(self.catalog, as_of=self.as_of)
        out = await svc.run_analysis(t.analysis_id, t.period, t.comparison, t.filters)
        res.period = f"{out['period']['label']} vs {out['comparison']['period']['label']}"
        res.agent_sql, res.evidence_id = out["executed_sql"], out["evidence_id"]
        a = D(out["change"])
        # display-level netting: the rounded drivers must still add to the rounded change
        shown = sum((D(d["contribution"]) for d in out["drivers"]), Decimal(0))
        if not out["nets_to_headline"] or shown != a:
            res.status, res.message = "fail", f"drivers do not net: Σ={shown} change={a}"
            res.agent_value = str(a)
            return
        p = svc.resolve_period(t.period)
        cp = FiscalCalendar(self.catalog.profile.calendar, self.as_of).comparison(p, out["comparison"]["kind"])
        params = period_params(self.cal, p) | period_params(self.cal, cp, "cmp_")
        analysis = self.catalog.analysis(t.analysis_id)
        sql, src = self._reference_sql(t)
        src = src or self._source_for(analysis.headline_metric)
        if sql:
            rows, res.reference_sql = await self._run_reference(sql, src, params)
            r, res.reference_kind = _scalar(rows), "sql"
        elif t.expected_value is not None:
            r, res.reference_kind = D(t.expected_value), "expected_value"
        else:
            res.status, res.message, res.agent_value = "skipped", "no reference for the headline change", str(a)
            return
        res.agent_value, res.reference_value = _num(a), _num(r)
        res.tolerance = str(t.tolerance_abs)
        if r is None:
            res.status, res.message = "fail", "reference returned no data"
            return
        res.difference = _num(a - r)
        res.status = "pass" if _within(a, r, t) else "fail"
        res.message = "change matches source; drivers net exactly" if res.status == "pass" else "change differs"


# ─────────────────────────────────────────────────────────────────────────────
# Reports
# ─────────────────────────────────────────────────────────────────────────────

def summarize(results: list[ReconResult]) -> dict[str, int]:
    out = {"total": len(results)}
    for s in ("pass", "fail", "error", "skipped"):
        out[s] = sum(1 for r in results if r.status == s)
    return out


def console_report(results: list[ReconResult]) -> str:
    icon = {"pass": "PASS", "fail": "FAIL", "error": "ERR ", "skipped": "SKIP"}
    lines = []
    for r in results:
        val = f"agent={r.agent_value} ref={r.reference_value}" if r.reference_value is not None else (r.agent_value or "")
        lines.append(f"[{icon[r.status]}] {r.test_id:<32} {r.period:<34} {val} {r.message}".rstrip())
        for mm in r.mismatches[:10]:
            lines.append(f"         ↳ {mm}")
    s = summarize(results)
    lines.append(f"\n{s['pass']}/{s['total']} passed · {s['fail']} failed · {s['error']} errors · {s['skipped']} skipped")
    return "\n".join(lines)


def write_reports(results: list[ReconResult], out_dir: str | Path, client_name: str = "") -> dict[str, str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    s = summarize(results)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    (out / "reconciliation.json").write_text(
        json.dumps({"client": client_name, "run_at": stamp, "summary": s,
                    "results": [asdict(r) for r in results]}, indent=2, default=str), encoding="utf-8")
    md = [f"# Source reconciliation — {client_name}", "", f"Run: {stamp}  ",
          f"**{s['pass']}/{s['total']} passed** · {s['fail']} failed · {s['error']} errors · {s['skipped']} skipped", "",
          "| Test | Kind | Period | Agent | Reference | Diff | Result |", "|---|---|---|---|---|---|---|"]
    for r in results:
        md.append(f"| {r.test_id} | {r.kind} | {r.period} | {r.agent_value or ''} | {r.reference_value or ''} "
                  f"| {r.difference or ''} | {r.status.upper()} {r.message} |")
    md += ["", "## Queries", ""]
    for r in results:
        md += [f"### {r.test_id}", "Agent path:", "```sql", *r.agent_sql, "```"]
        if r.reference_sql:
            md += ["Reference:", "```sql", r.reference_sql, "```"]
        for mm in r.mismatches:
            md.append(f"- mismatch {mm}")
        md.append("")
    (out / "reconciliation.md").write_text("\n".join(md), encoding="utf-8")
    cases = []
    for r in results:
        body = ""
        if r.status == "fail":
            body = f'<failure message="{escape(r.message or "mismatch")}">agent={escape(str(r.agent_value))} ' \
                   f'reference={escape(str(r.reference_value))} {escape(json.dumps(r.mismatches))}</failure>'
        elif r.status == "error":
            body = f'<error message="{escape(r.message)}"/>'
        elif r.status == "skipped":
            body = f'<skipped message="{escape(r.message)}"/>'
        cases.append(f'<testcase classname="reconciliation.{r.kind}" name="{escape(r.test_id)}" '
                     f'time="{r.duration_ms / 1000:.3f}">{body}</testcase>')
    (out / "reconciliation.junit.xml").write_text(
        f'<?xml version="1.0" encoding="UTF-8"?><testsuite name="reconciliation" tests="{s["total"]}" '
        f'failures="{s["fail"]}" errors="{s["error"]}" skipped="{s["skipped"]}">{"".join(cases)}</testsuite>',
        encoding="utf-8")
    return {"json": str(out / "reconciliation.json"), "markdown": str(out / "reconciliation.md"),
            "junit": str(out / "reconciliation.junit.xml")}
