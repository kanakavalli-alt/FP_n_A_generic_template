"""
Deterministic SQL compiler: semantic-layer metric definition -> parameterized
SQL for one source dialect.

This replaces Kaplan's hand-written `*_queries.py` files. The guarantees are the
same ("the LLM never writes headline SQL; identical inputs produce identical
SQL; every value is bound, never interpolated"), but the SQL now comes from
reviewed catalog rows instead of reviewed Python.

Safety model (defence in depth — the primary control is that catalog rows are
reviewed and only `confirmed`/`partial` metrics are queryable):
  * identifiers must match a strict regex;
  * measure / dimension expressions are tokenised and may only contain
    identifiers, numbers, quoted literals, operators and an allow-list of SQL
    functions/keywords — never a statement keyword, comment or separator;
  * every filter value and period bound is a bound parameter.

The same compiler runs inside the MCP server (mcp_server/server.py), which
re-compiles the QuerySpec it receives, so the server never executes SQL text
it was handed for a headline number.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from fpa_agent.semantic.models import Filter, Join, TimeBinding

Dialect = Literal["sqlite", "mssql", "postgres", "redshift", "oracle", "snowflake", "bigquery", "mysql"]


class QueryCompileError(ValueError):
    """Unsafe, invalid or unsupported query input — never silently coerced."""


# ─────────────────────────────────────────────────────────────────────────────
# Expression validation
# ─────────────────────────────────────────────────────────────────────────────

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){0,2}$")
_TOKEN_RE = re.compile(
    r"\s+|'(?:[^']|'')*'|\d+(?:\.\d+)?|[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
    r"|<>|!=|>=|<=|\|\||[-+*/(),=<>%]"
)
_ALLOWED_WORDS = {
    "SUM", "COUNT", "AVG", "MIN", "MAX", "DISTINCT", "COALESCE", "NULLIF", "ABS",
    "ROUND", "CASE", "WHEN", "THEN", "ELSE", "END", "AND", "OR", "NOT", "IN", "IS",
    "NULL", "LIKE", "BETWEEN", "CAST", "AS", "INTEGER", "INT", "BIGINT", "DECIMAL",
    "NUMERIC", "FLOAT", "REAL", "VARCHAR", "DATE", "EXTRACT", "YEAR", "MONTH", "DAY",
    "FROM", "UPPER", "LOWER", "TRIM", "SIGN", "FLOOR", "CEIL", "CEILING", "IIF",
    "NVL", "ISNULL", "IFNULL", "TRUE", "FALSE", "CONCAT", "SUBSTRING", "SUBSTR",
}
_DENIED_WORDS = {
    "SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "DROP", "ALTER", "CREATE",
    "TRUNCATE", "GRANT", "REVOKE", "EXEC", "EXECUTE", "CALL", "UNION", "INTO",
    "DECLARE", "WAITFOR", "SHUTDOWN", "PRAGMA", "ATTACH", "DETACH", "COPY",
    "UNLOAD", "REPLACE", "UPSERT", "SET", "BEGIN", "COMMIT", "ROLLBACK", "USE",
}


def validate_identifier(name: str, what: str) -> str:
    if not isinstance(name, str) or not _IDENT_RE.match(name.strip()):
        raise QueryCompileError(f"invalid {what}: {name!r}")
    return name.strip()


def validate_expression(expr: str, what: str, known_aliases: set[str] | None = None) -> str:
    """Validate a SQL scalar/aggregate expression from the catalog."""
    if not isinstance(expr, str) or not expr.strip():
        raise QueryCompileError(f"empty {what}")
    if ";" in expr or "--" in expr or "/*" in expr or "*/" in expr:
        raise QueryCompileError(f"{what} contains a statement separator or comment: {expr!r}")
    pos = 0
    depth = 0
    while pos < len(expr):
        m = _TOKEN_RE.match(expr, pos)
        if not m:
            raise QueryCompileError(f"{what} has an unexpected character at {pos}: {expr!r}")
        tok = m.group(0)
        pos = m.end()
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
            if depth < 0:
                raise QueryCompileError(f"{what} has unbalanced parentheses: {expr!r}")
        if tok[0].isalpha() or tok[0] == "_":
            up = tok.upper()
            if up in _DENIED_WORDS:
                raise QueryCompileError(f"{what} uses a forbidden keyword {tok!r}: {expr!r}")
            if up in _ALLOWED_WORDS:
                continue
            validate_identifier(tok, f"identifier in {what}")
    if depth != 0:
        raise QueryCompileError(f"{what} has unbalanced parentheses: {expr!r}")
    return expr.strip()


# ─────────────────────────────────────────────────────────────────────────────
# QuerySpec — the structured request (agent -> MCP server). Built by code from
# a catalog row; the model never constructs one.
# ─────────────────────────────────────────────────────────────────────────────

class QuerySpec(BaseModel):
    dialect: Dialect
    table: str
    table_alias: str | None = None
    joins: list[Join] = Field(default_factory=list)
    measures: dict[str, str]                       # output alias -> aggregate expr
    filters: list[Filter] = Field(default_factory=list)
    time: TimeBinding = Field(default_factory=TimeBinding)
    period_start: date | None = None               # inclusive
    period_end: date | None = None                 # exclusive
    fiscal_years: list[int] = Field(default_factory=list)   # time.pattern == fiscal_year
    snapshot: bool = False
    group_by: dict[str, str] = Field(default_factory=dict)  # output alias -> column expr
    time_grain: Literal["month"] | None = None
    distinct_values_of: str | None = None          # dimension-value listing mode
    limit: int | None = None


class CompiledQuery(BaseModel):
    sql: str
    params: list[Any]
    dialect: str
    display_sql: str                               # SQL with values inlined, for humans only
    output_columns: list[str]


_FILTER_OPS = {"=", "!=", "<>", ">", "<", ">=", "<=", "IN", "NOT IN", "LIKE",
               "IS NULL", "IS NOT NULL", "BETWEEN"}

PERIOD_ALIAS = "period_yyyymm"


def _month_bucket(dialect: str, col: str) -> str:
    if dialect == "sqlite":
        return f"CAST(strftime('%Y%m', {col}) AS INTEGER)"
    if dialect == "mssql":
        return f"(YEAR({col}) * 100 + MONTH({col}))"
    return f"(EXTRACT(YEAR FROM {col}) * 100 + EXTRACT(MONTH FROM {col}))"


def _date_param(dialect: str, d: date):
    # sqlite stores dates as ISO text; every other driver binds a real date.
    return d.isoformat() if dialect == "sqlite" else d


def _key_param(fmt: str, yyyymm: int):
    y, m = divmod(yyyymm, 100)
    if fmt == "YYYYMM":
        return yyyymm
    return f"{y:04d}{'_' if fmt == 'YYYY_MM' else '-'}{m:02d}"


def _yyyymm(d: date) -> int:
    return d.year * 100 + d.month


def _period_predicate(spec: QuerySpec) -> tuple[str, list, str | None]:
    """Returns (predicate_sql, params, month_bucket_expr)."""
    t = spec.time
    if t.pattern == "none":
        return "", [], None
    if spec.period_start is None or spec.period_end is None:
        raise QueryCompileError("a time-bound metric needs period_start/period_end")
    start, end = spec.period_start, spec.period_end
    last_month_start = None
    if end.day == 1:
        last_month_start = date(end.year, end.month - 1, 1) if end.month > 1 else date(end.year - 1, 12, 1)

    if t.pattern == "date":
        col = validate_expression(t.column or "", "time column")
        bucket = _month_bucket(spec.dialect, col)
        if spec.snapshot and spec.time_grain is None:
            if last_month_start is None:
                raise QueryCompileError("snapshot metrics need month-aligned periods")
            start = last_month_start
        return (f"{col} >= ? AND {col} < ?",
                [_date_param(spec.dialect, start), _date_param(spec.dialect, end)], bucket)

    if not (start.day == 1 and end.day == 1):
        raise QueryCompileError(f"time pattern {t.pattern!r} needs a month-aligned period")
    s_key, e_key = _yyyymm(start), _yyyymm(last_month_start)
    if spec.snapshot and spec.time_grain is None:
        s_key = e_key

    if t.pattern == "year_month":
        y = validate_expression(t.year_column or "", "year column")
        m = validate_expression(t.month_column or "", "month column")
        expr = f"({y} * 100 + {m})"
        return f"{expr} BETWEEN ? AND ?", [s_key, e_key], expr

    if t.pattern == "period_key":
        col = validate_expression(t.column or "", "period key column")
        return (f"{col} BETWEEN ? AND ?",
                [_key_param(t.key_format, s_key), _key_param(t.key_format, e_key)], col)

    if t.pattern == "fiscal_year":
        col = validate_expression(t.column or "", "fiscal year column")
        if not spec.fiscal_years:
            raise QueryCompileError("fiscal_year metrics need whole fiscal years")
        if spec.time_grain:
            raise QueryCompileError("fiscal_year metrics have no monthly grain")
        return f"{col} BETWEEN ? AND ?", [min(spec.fiscal_years), max(spec.fiscal_years)], None

    raise QueryCompileError(f"unsupported time pattern {t.pattern!r}")


def _filter_sql(filters: list[Filter]) -> tuple[list[str], list]:
    clauses, params = [], []
    for f in filters:
        col = validate_expression(f.column, "filter column")
        op = (f.operator or "=").upper().strip()
        if op not in _FILTER_OPS:
            raise QueryCompileError(f"unsupported filter operator {op!r}")
        vals = f.values
        if op in ("IN", "NOT IN"):
            if not vals:
                raise QueryCompileError(f"{op} filter on {col} needs values")
            clauses.append(f"{col} {op} ({', '.join('?' * len(vals))})")
            params.extend(vals)
        elif op in ("IS NULL", "IS NOT NULL"):
            clauses.append(f"{col} {op}")
        elif op == "BETWEEN":
            if len(vals) != 2:
                raise QueryCompileError(f"BETWEEN filter on {col} needs exactly two values")
            clauses.append(f"{col} BETWEEN ? AND ?")
            params.extend(vals)
        else:
            if len(vals) != 1:
                raise QueryCompileError(f"{op} filter on {col} needs exactly one value")
            clauses.append(f"{col} {op} ?")
            params.append(vals[0])
    return clauses, params


def compile_spec(spec: QuerySpec) -> CompiledQuery:
    table = validate_identifier(spec.table, "table")
    alias = validate_identifier(spec.table_alias, "table alias") if spec.table_alias else None
    from_sql = f"{table} {alias}" if alias else table
    for j in spec.joins:
        jt = validate_identifier(j.table, "join table")
        ja = validate_identifier(j.alias, "join alias")
        if not j.on:
            raise QueryCompileError(f"join {jt} has no ON columns")
        on = " AND ".join(
            f"{validate_identifier(l, 'join column')} = {validate_identifier(r, 'join column')}"
            for l, r in j.on
        )
        from_sql += f" {'LEFT JOIN' if j.type == 'left' else 'JOIN'} {jt} {ja} ON {on}"

    period_sql, period_params, bucket = _period_predicate(spec)
    filter_clauses, filter_params = _filter_sql(spec.filters)
    where = [c for c in [period_sql, *filter_clauses] if c]

    select: list[tuple[str, str]] = []           # (expr, alias)
    if spec.time_grain == "month":
        if bucket is None:
            raise QueryCompileError("this metric has no month grain")
        select.append((bucket, PERIOD_ALIAS))
    for out_alias, expr in spec.group_by.items():
        select.append((validate_expression(expr, f"dimension {out_alias}"),
                       validate_identifier(out_alias, "output alias")))

    if spec.distinct_values_of:
        expr = validate_expression(spec.distinct_values_of, "dimension column")
        sql = f"SELECT DISTINCT {expr} AS value FROM {from_sql}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY 1"
        out_cols = ["value"]
    else:
        if not spec.measures:
            raise QueryCompileError("no measures to compute")
        group_exprs = [e for e, _ in select]
        for out_alias, expr in spec.measures.items():
            select.append((validate_expression(expr, f"measure {out_alias}"),
                           validate_identifier(out_alias, "output alias")))
        sql = "SELECT " + ", ".join(f"{e} AS {a}" for e, a in select) + f" FROM {from_sql}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        if group_exprs:
            sql += " GROUP BY " + ", ".join(group_exprs)
            sql += " ORDER BY " + ", ".join(str(i + 1) for i in range(len(group_exprs)))
        out_cols = [a for _, a in select]

    if spec.limit:
        sql = _apply_limit(spec.dialect, sql, int(spec.limit))

    params = period_params + filter_params
    return CompiledQuery(
        sql=convert_placeholders(sql, spec.dialect),
        params=params,
        dialect=spec.dialect,
        display_sql=inline_values(sql, params),
        output_columns=out_cols,
    )


def _apply_limit(dialect: str, sql: str, n: int) -> str:
    if dialect == "mssql":
        return sql.replace("SELECT ", f"SELECT TOP {n} ", 1)
    if dialect == "oracle":
        return f"{sql} FETCH FIRST {n} ROWS ONLY"
    return f"{sql} LIMIT {n}"


# ─────────────────────────────────────────────────────────────────────────────
# Placeholder handling — quote-aware, so a '?' or '%' inside a string literal in
# a catalog expression is never mistaken for a bind marker.
# ─────────────────────────────────────────────────────────────────────────────

def _split_literals(sql: str) -> list[tuple[bool, str]]:
    """[(is_literal, text), ...]"""
    out, buf, i, in_lit = [], [], 0, False
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            if in_lit and i + 1 < len(sql) and sql[i + 1] == "'":
                buf.append("''"); i += 2; continue
            if in_lit:
                buf.append(ch); out.append((True, "".join(buf))); buf = []; in_lit = False
            else:
                if buf:
                    out.append((False, "".join(buf)))
                buf = [ch]; in_lit = True
        else:
            buf.append(ch)
        i += 1
    if buf:
        out.append((in_lit, "".join(buf)))
    return out


def convert_placeholders(sql: str, dialect: str) -> str:
    """'?' (internal) -> the driver's paramstyle."""
    parts = _split_literals(sql)
    if dialect in ("sqlite", "mssql"):
        return sql
    out, n = [], 0
    for is_lit, text in parts:
        if dialect in ("postgres", "redshift", "mysql", "snowflake"):
            text = text.replace("%", "%%")
            if not is_lit:
                text = text.replace("?", "%s")
        elif not is_lit:
            pieces = text.split("?")
            rebuilt = pieces[0]
            for p in pieces[1:]:
                n += 1
                rebuilt += (f":{n}" if dialect == "oracle" else f"@p{n}") + p
            text = rebuilt
        out.append(text)
    return "".join(out)


def _literal(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def inline_values(sql_with_qmarks: str, params: list) -> str:
    """For DISPLAY/audit only: substitute bound values into '?' markers so a
    human can re-run the exact query. Never executed by this codebase."""
    it = iter(params)
    out = []
    for is_lit, text in _split_literals(sql_with_qmarks):
        if not is_lit:
            text = re.sub(r"\?", lambda _: _literal(next(it, None)), text)
        out.append(text)
    return "".join(out)


_READ_ONLY_FIRST = re.compile(r"^\s*(\(\s*)*(SELECT|WITH)\b", re.IGNORECASE)
_WRITE_WORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|EXEC|"
    r"EXECUTE|CALL|INTO|ATTACH|PRAGMA|COPY|UNLOAD|SHUTDOWN|WAITFOR)\b",
    re.IGNORECASE,
)


def assert_read_only(sql: str) -> str:
    """Guard for exploratory / reference SQL: a single SELECT/WITH statement,
    no write keywords anywhere (including DML hidden inside a CTE)."""
    body = "".join(t for is_lit, t in _split_literals(sql) if not is_lit)
    stripped = body.strip().rstrip(";")
    if ";" in stripped:
        raise QueryCompileError("only a single statement is allowed")
    if "--" in body or "/*" in body:
        raise QueryCompileError("comments are not allowed in exploratory SQL")
    if not _READ_ONLY_FIRST.match(stripped):
        raise QueryCompileError("only SELECT / WITH queries are allowed")
    if _WRITE_WORDS.search(stripped):
        raise QueryCompileError("write / DDL keywords are not allowed")
    return sql.strip().rstrip(";")
