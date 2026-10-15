"""MetricService against the demo sources — each value is cross-checked with
hand-written SQL run directly on the database (independent of the compiler)."""
from decimal import Decimal as D

import pytest

from fpa_agent.engine.methods import MethodError
from fpa_agent.engine.service import GateError, MetricService, QueryError
from tests.conftest import AS_OF

Q3_FY26 = {"type": "quarter", "fiscal": True, "year": 2026, "quarter": 3}
SEP26 = {"type": "month", "year": 2026, "month": 9}


@pytest.fixture
def svc(catalog):
    return MetricService(catalog, as_of=AS_OF)


def test_base_metric_matches_raw_sql(svc, run, sql):
    out = run(svc.get_metric("new_starts", Q3_FY26))
    raw = sql("sis", "SELECT SUM(starts) FROM enrollment_funnel WHERE is_reportable=1 AND yr=2026 AND mnth<=3")[0][0]
    assert D(out["value"]) == raw and out["evidence_id"].startswith("ev_")
    assert "is_reportable = 1" in out["executed_sql"][0]  # the confirmed filter is in the executed SQL


def test_filter_and_breakdown(svc, run, sql):
    out = run(svc.get_metric("leads", SEP26, filters={"program": ["Nursing"]}, group_by=["channel"]))
    raw = dict(sql("sis", "SELECT channel, SUM(leads) FROM enrollment_funnel WHERE is_reportable=1 AND yr=2026 "
                          "AND mnth=9 AND program='Nursing' GROUP BY channel"))
    assert {r["channel"]: int(r["value"]) for r in out["rows"]} == raw


def test_derived_metric_is_ratio_of_sums(svc, run, sql):
    out = run(svc.get_metric("conversion_rate", SEP26))
    s, l = sql("sis", "SELECT SUM(starts), SUM(leads) FROM enrollment_funnel WHERE is_reportable=1 AND yr=2026 AND mnth=9")[0]
    assert D(out["value"]) == (D(s) / D(l)).quantize(D("0.0001"))


def test_snapshot_takes_last_month(svc, run, sql):
    out = run(svc.get_metric("active_students", {"type": "quarter", "fiscal": True, "year": 2026, "quarter": 4}))
    raw = sql("sis", "SELECT SUM(active_students) FROM student_census WHERE snapshot_month='2026-06'")[0][0]
    assert int(out["value"]) == raw


def test_monthly_trend(svc, run):
    out = run(svc.get_metric("net_revenue", {"type": "last_n_months", "n": 3}, time_grain="month"))
    assert [r["period"] for r in out["rows"]] == [202607, 202608, 202609]


def test_fiscal_year_only_metric_rejects_quarters(svc, run):
    with pytest.raises(QueryError):
        run(svc.get_metric("fy_tuition_revenue", Q3_FY26))


def test_unknown_dimension_is_a_clear_error(svc, run):
    with pytest.raises(QueryError, match="cannot be broken down by 'modality'"):
        run(svc.get_metric("leads", SEP26, group_by=["modality"]))


@pytest.mark.parametrize("metric,status", [("marketing_spend", "blocked"), ("retention_rate", "incomplete"),
                                           ("student_satisfaction", "causal")])
def test_governance_gates(svc, run, metric, status):
    with pytest.raises(GateError) as e:
        run(svc.get_metric(metric, SEP26))
    assert e.value.status == status


def test_partial_definition_adds_caveat(svc, run):
    out = run(svc.get_metric("revenue_per_student", {"type": "year", "fiscal": True, "year": 2026}))
    assert any("provisional" in c for c in out["caveats"])


def test_open_period_caveat(svc, run):
    out = run(svc.get_metric("leads", {"type": "month", "year": 2026, "month": 10}))
    assert any("not closed" in c for c in out["caveats"])


@pytest.mark.parametrize("analysis,period", [
    ("new_starts_drivers", Q3_FY26), ("census_bridge", SEP26),
    ("revenue_per_student_bridge", {"type": "year", "fiscal": True, "year": 2026}),
    ("revenue_vs_budget", {"type": "month", "year": 2026, "month": 8}),
    ("revenue_price_volume", {"type": "ytd"}),
])
def test_every_demo_analysis_nets_after_display_rounding(svc, run, analysis, period):
    out = run(svc.run_analysis(analysis, period))
    assert out["nets_to_headline"] is True
    assert sum(D(d["contribution"]) for d in out["drivers"]) == D(out["change"])
    assert out["drivers_table"].startswith("| Driver |")
    assert out["input_evidence_ids"]


def test_starts_bridge_unattributed_is_reentries(svc, run, sql):
    out = run(svc.run_analysis("new_starts_drivers", Q3_FY26))
    reentry = {d["role"]: D(d["contribution"]) for d in out["drivers"]}["unattributed"]
    cy = sql("sis", "SELECT SUM(starts) FROM enrollment_funnel WHERE channel='Re-entry' AND yr=2026 AND mnth<=3")[0][0]
    py = sql("sis", "SELECT SUM(starts) FROM enrollment_funnel WHERE channel='Re-entry' AND yr=2025 AND mnth<=3")[0][0]
    assert abs(reentry - (cy - py)) <= 1  # display-rounded


def test_budget_comparison_uses_budget_metric(svc, run, sql):
    out = run(svc.compare_metric("net_revenue", {"type": "month", "year": 2026, "month": 8}, "budget"))
    b = sql("erp", "SELECT SUM(budget_net_sales) FROM budget WHERE period_key=202608")[0][0]
    assert D(out["comparison_value"]) == D(str(b)).quantize(D("0.01"))
    assert out["comparison"]["period"]["label"].startswith("Budget")


def test_rollforward_rejects_budget(svc, run):
    with pytest.raises(MethodError):
        run(svc.run_analysis("census_bridge", SEP26, "budget"))


def test_roadmap_analysis_is_declined(svc, run):
    with pytest.raises(GateError):
        run(svc.run_analysis("retention_drivers", SEP26))


def test_adhoc_breakdown_for_any_metric(svc, run):
    out = run(svc.compare_metric("units_sold", SEP26, "prior_year", breakdown="modality"))
    assert {d["driver"] for d in out["drivers"]} == {"Online", "Campus", "Hybrid"}


def test_evidence_chain(svc, run):
    out = run(svc.run_analysis("revenue_per_student_bridge", {"type": "year", "fiscal": True, "year": 2026}))
    chain = svc.ledger.lineage(out["evidence_id"])
    kinds = [e.kind for e in chain]
    assert kinds[0] == "analysis" and kinds.count("metric_query") == 4
    assert all(e.executed_sql for e in chain if e.kind == "metric_query")
