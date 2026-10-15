from datetime import date

import pytest

from fpa_agent.query.compiler import (
    QueryCompileError, QuerySpec, assert_read_only, compile_spec, convert_placeholders, inline_values,
)
from fpa_agent.semantic.models import Filter, Join, TimeBinding


def spec(**kw):
    base = dict(dialect="mssql", table="dbo.fact", measures={"rev": "SUM(amount)"},
                time=TimeBinding(pattern="date", column="txn_date"),
                period_start=date(2026, 1, 1), period_end=date(2026, 4, 1))
    base.update(kw)
    return QuerySpec(**base)


def test_basic_date_pattern_binds_values():
    c = compile_spec(spec(filters=[Filter(column="region", operator="IN", values=["EU", "US"])]))
    assert c.sql == ("SELECT SUM(amount) AS rev FROM dbo.fact WHERE txn_date >= ? AND txn_date < ? "
                     "AND region IN (?, ?)")
    assert c.params == [date(2026, 1, 1), date(2026, 4, 1), "EU", "US"]
    assert "'EU'" in c.display_sql


def test_year_month_snapshot_uses_last_month_only():
    c = compile_spec(spec(dialect="postgres", time=TimeBinding(pattern="year_month", year_column="yr", month_column="mn"),
                          snapshot=True))
    assert "BETWEEN %s AND %s" in c.sql and c.params == [202603, 202603]


def test_period_key_string_format():
    c = compile_spec(spec(dialect="sqlite", time=TimeBinding(pattern="period_key", column="pk", key_format="YYYY_MM")))
    assert c.params == ["2026_01", "2026_03"]


def test_group_by_and_month_grain():
    c = compile_spec(spec(dialect="mssql", group_by={"region": "region"}, time_grain="month"))
    assert "(YEAR(txn_date) * 100 + MONTH(txn_date)) AS period_yyyymm" in c.sql
    assert c.sql.endswith("GROUP BY (YEAR(txn_date) * 100 + MONTH(txn_date)), region ORDER BY 1, 2")


def test_joins():
    c = compile_spec(spec(table="fact", table_alias="f",
                          joins=[Join(table="dim_date", alias="d", on=[["f.date_key", "d.date_key"]])],
                          measures={"rev": "SUM(f.amount)"}, time=TimeBinding(pattern="year_month",
                                                                              year_column="d.yr", month_column="d.mn")))
    assert "FROM fact f JOIN dim_date d ON f.date_key = d.date_key" in c.sql


@pytest.mark.parametrize("dialect,marker", [("oracle", ":1"), ("bigquery", "@p1"), ("redshift", "%s"), ("sqlite", "?")])
def test_paramstyles(dialect, marker):
    assert marker in compile_spec(spec(dialect=dialect)).sql


def test_placeholders_inside_literals_untouched():
    sql = "SELECT SUM(CASE WHEN x = 'a?b%' THEN 1 END) FROM t WHERE y = ?"
    assert convert_placeholders(sql, "oracle") == "SELECT SUM(CASE WHEN x = 'a?b%' THEN 1 END) FROM t WHERE y = :1"
    assert convert_placeholders(sql, "postgres").endswith("'a?b%%' THEN 1 END) FROM t WHERE y = %s")
    assert inline_values(sql, [5]).endswith("WHERE y = 5")


@pytest.mark.parametrize("bad", [
    "SUM(x); DROP TABLE t", "SUM(x) -- c", "(SELECT MAX(a) FROM b)", "SUM(x) UNION SELECT 1",
    "EXEC sp_who", "SUM(x)/*c*/", "SUM(x", "1; DELETE FROM t",
])
def test_unsafe_measures_rejected(bad):
    with pytest.raises(QueryCompileError):
        compile_spec(spec(measures={"m": bad}))


@pytest.mark.parametrize("bad_ident", ["fact; drop", "fact--", "a.b.c.d", "1abc", "fa ct"])
def test_unsafe_identifiers_rejected(bad_ident):
    with pytest.raises(QueryCompileError):
        compile_spec(spec(table=bad_ident))


def test_case_expression_allowed():
    c = compile_spec(spec(measures={"m": "SUM(CASE WHEN status IN ('A','B') THEN amount ELSE 0 END)"}))
    assert "CASE WHEN status IN ('A','B')" in c.sql


def test_non_month_aligned_rejected_for_monthly_patterns():
    with pytest.raises(QueryCompileError):
        compile_spec(spec(time=TimeBinding(pattern="year_month", year_column="y", month_column="m"),
                          period_start=date(2026, 1, 15)))


def test_filter_operator_whitelist():
    with pytest.raises(QueryCompileError):
        compile_spec(spec(filters=[Filter(column="a", operator="; DROP", values=[1])]))


@pytest.mark.parametrize("sql", [
    "SELECT * FROM t; DELETE FROM t", "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",
    "UPDATE t SET a=1", "SELECT * INTO new_t FROM t", "SELECT 1 -- hi",
])
def test_read_only_guard_rejects(sql):
    with pytest.raises(QueryCompileError):
        assert_read_only(sql)


def test_read_only_guard_allows_cte_and_literals():
    assert assert_read_only("WITH a AS (SELECT 1 AS v) SELECT v, 'delete; update' AS note FROM a;")
