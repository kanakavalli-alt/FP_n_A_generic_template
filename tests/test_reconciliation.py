"""The reconciliation module itself: all catalog tests pass on the demo, and a
wrong number is actually CAUGHT (a test that can't fail proves nothing)."""
import json

from fpa_agent.semantic.models import ReconTest
from fpa_agent.testing.reconcile import ReconciliationRunner, summarize, write_reports
from tests.conftest import AS_OF


def test_all_catalog_reconciliation_tests_pass(catalog, run):
    runner = ReconciliationRunner(catalog, as_of=AS_OF)
    results = run(runner.run(runner.select()))
    failed = [(r.test_id, r.message, r.agent_value, r.reference_value) for r in results if r.status != "pass"]
    assert not failed, failed
    assert summarize(results)["pass"] == len(results) >= 13


def test_auto_generated_tests_pass(catalog, run):
    runner = ReconciliationRunner(catalog, as_of=AS_OF)
    tests = runner.auto_tests(months=2)
    assert len(tests) >= 14
    assert all(r.status == "pass" for r in run(runner.run(tests)))


def test_wrong_reference_is_detected(catalog, run):
    runner = ReconciliationRunner(catalog, as_of=AS_OF)
    t = ReconTest(test_id="bad", metric_id="leads", period={"type": "month", "year": 2026, "month": 9},
                  # forgets the is_reportable filter — exactly the Kaplan "starts didn't tie" class of bug
                  reference_sql="SELECT SUM(leads) AS value FROM enrollment_funnel "
                                "WHERE yr * 100 + mnth BETWEEN :start_yyyymm AND :end_yyyymm")
    r = run(runner.run_one(t))
    assert r.status == "fail" and r.difference and int(r.difference) < 0


def test_grouped_mismatch_reports_the_group(catalog, run):
    runner = ReconciliationRunner(catalog, as_of=AS_OF)
    t = ReconTest(test_id="bad_groups", metric_id="new_starts", group_by=["channel"],
                  period={"type": "month", "year": 2026, "month": 9},
                  reference_sql="SELECT channel, SUM(starts) + CASE WHEN channel = 'Organic' THEN 1 ELSE 0 END "
                                "AS value FROM enrollment_funnel WHERE is_reportable = 1 AND yr = 2026 "
                                "AND mnth = 9 GROUP BY channel")
    r = run(runner.run_one(t))
    assert r.status == "fail" and [m["group"] for m in r.mismatches] == [{"channel": "Organic"}]


def test_expected_value_reference_and_tolerance(catalog, run):
    runner = ReconciliationRunner(catalog, as_of=AS_OF)
    base = dict(metric_id="net_revenue", period={"type": "month", "year": 2026, "month": 9})
    agent = run(runner.run_one(ReconTest(test_id="x", expected_value=0, **base))).agent_value
    ok = run(runner.run_one(ReconTest(test_id="ok", expected_value=float(agent) + 0.4, tolerance_abs=0.5, **base)))
    bad = run(runner.run_one(ReconTest(test_id="bad", expected_value=float(agent) + 0.6, tolerance_abs=0.5, **base)))
    assert (ok.status, bad.status) == ("pass", "fail")


def test_reports_written(catalog, run, tmp_path):
    runner = ReconciliationRunner(catalog, as_of=AS_OF)
    results = run(runner.run(runner.select(tags=["smoke"])))
    paths = write_reports(results, tmp_path, catalog.profile.name)
    data = json.loads(open(paths["json"], encoding="utf-8").read())
    assert data["summary"]["pass"] == len(results)
    assert "<testsuite" in open(paths["junit"], encoding="utf-8").read()
