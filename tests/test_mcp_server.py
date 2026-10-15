"""The generic MCP server's tool functions, against the demo SIS database."""
import importlib
import sys
from datetime import date

import pytest

pytest.importorskip("mcp")

from fpa_agent.engine.service import MetricService  # noqa: E402
from tests.conftest import AS_OF, ROOT  # noqa: E402


@pytest.fixture
def server(demo_dbs, monkeypatch):
    monkeypatch.setenv("SOURCE_ID", "sis")
    monkeypatch.setenv("DIALECT", "sqlite")
    monkeypatch.setenv("DB_DRIVER", "sqlite")
    monkeypatch.setenv("DB_CONN", str(demo_dbs["sis"]).replace("\\", "/"))
    monkeypatch.setenv("ALLOWED_TABLES", "enrollment_funnel,student_census,status_events,fy_enrollment_summary")
    monkeypatch.setenv("LOG_DIR", str(ROOT / "out" / "test_logs"))
    sys.path.insert(0, str(ROOT / "mcp_server"))
    sys.modules.pop("mcp_server.server", None)
    return importlib.import_module("mcp_server.server")


def test_query_spec_round_trip_matches_engine(server, catalog, run):
    svc = MetricService(catalog, as_of=AS_OF)
    m = catalog.metric("new_starts")
    p = svc.resolve_period({"type": "month", "year": 2026, "month": 9})
    spec = svc.build_spec(m, p, {}, ["channel"], None)
    out = run(server.run_query_spec(spec.model_dump(mode="json")))
    assert out["status"] == "success" and len(out["rows"]) == 6
    direct = run(svc.fetch("new_starts", p, {}, ["channel"]))
    assert {r["channel"]: r["new_starts"] for r in out["rows"]} == {k[0]: int(v) for k, v in direct.groups.items()}


def test_server_rejects_writes_and_unlisted_tables(server, run):
    assert run(server.run_readonly_sql("DELETE FROM enrollment_funnel"))["status"] == "error"
    assert run(server.run_readonly_sql("SELECT * FROM sqlite_master"))["status"] == "error"
    ok = run(server.run_readonly_sql("SELECT COUNT(*) AS n FROM student_census"))
    assert ok["status"] == "success" and ok["exploratory"] is True


def test_server_rejects_spec_for_other_table(server, run):
    spec = {"dialect": "sqlite", "table": "secret_payroll", "measures": {"x": "SUM(salary)"},
            "time": {"pattern": "none"}}
    assert run(server.run_query_spec(spec))["status"] == "error"


def test_reference_sql_named_params(server, run):
    out = run(server.run_reference_sql(
        "SELECT SUM(active_students) AS value FROM student_census WHERE snapshot_month = :end_ym",
        {"end_ym": "2026-09"}))
    assert out["status"] == "success" and out["rows"][0]["value"] == 8504
