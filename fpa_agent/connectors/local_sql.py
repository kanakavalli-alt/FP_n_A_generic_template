"""Direct DB-API connector. Used for local development, the demo, CI and the
reconciliation runner when it can reach the database directly.

connection config (sl_sources.connection), secrets come from env only:
    {"mode": "local", "driver": "sqlite", "path_env": "DEMO_ERP_DB"}
    {"mode": "local", "driver": "pyodbc", "dsn_env": "ERP_ODBC_DSN"}
    {"mode": "local", "driver": "redshift_connector", "kwargs_env": "RS_CONN_JSON"}
    {"mode": "local", "driver": "oracledb", "kwargs_env": "ORA_CONN_JSON"}
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import sqlite3
import threading
import time
from typing import Any, Callable

from fpa_agent.connectors.base import Connector, QueryResult, to_jsonable
from fpa_agent.query.compiler import (
    QuerySpec, _split_literals, assert_read_only, compile_spec, convert_placeholders,
    inline_values, validate_identifier,
)

_NAMED_RE = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")


def bind_named(sql: str, values: dict[str, Any], dialect: str) -> tuple[str, list]:
    """':name' placeholders (reference SQL convention) -> driver placeholders +
    ordered params. Only names present in `values` are replaced."""
    order: list[str] = []

    def repl(m):
        name = m.group(1)
        if name not in values:
            return m.group(0)
        order.append(name)
        return "?"

    qmark_sql = "".join(
        text if is_lit else _NAMED_RE.sub(repl, text) for is_lit, text in _split_literals(sql)
    )
    return convert_placeholders(qmark_sql, dialect), [values[n] for n in order]


class LocalSqlConnector(Connector):
    def __init__(self, source_id: str, dialect: str, connect: Callable[[], Any]):
        self.source_id = source_id
        self.dialect = dialect
        self._connect = connect
        self._conn = None
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, source_id: str, dialect: str, cfg: dict) -> "LocalSqlConnector":
        driver = cfg.get("driver", "sqlite")
        if driver == "sqlite":
            path = os.getenv(cfg.get("path_env", ""), "") or cfg.get("path", "")
            if not path:
                raise ValueError(f"source {source_id}: sqlite path not configured")
            if not os.path.isabs(path):
                from fpa_agent.config import PROJECT_ROOT
                path = str(PROJECT_ROOT / path)
            if not os.path.exists(path):
                raise FileNotFoundError(f"source {source_id}: {path} not found "
                                        f"(run: python demo/build_demo_db.py)")
            path = path.replace("\\", "/")
            # read-only URI: the connector can never write to the source.
            return cls(source_id, dialect,
                       lambda: sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False))
        module = importlib.import_module(driver)
        if cfg.get("dsn_env"):
            dsn = os.environ[cfg["dsn_env"]]
            return cls(source_id, dialect, lambda: module.connect(dsn))
        kwargs = json.loads(os.environ[cfg["kwargs_env"]]) if cfg.get("kwargs_env") else {}
        return cls(source_id, dialect, lambda: module.connect(**kwargs))

    def _conn_or_connect(self):
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def _execute(self, sql: str, params: list, display: str, max_rows: int | None) -> QueryResult:
        t0 = time.perf_counter()
        with self._lock:
            cur = self._conn_or_connect().cursor()
            try:
                cur.execute(sql, params)
                cols = [d[0] for d in cur.description or []]
                fetched = cur.fetchmany(max_rows + 1) if max_rows else cur.fetchall()
            finally:
                cur.close()
        truncated = bool(max_rows) and len(fetched) > max_rows
        rows = [{c: to_jsonable(v) for c, v in zip(cols, r)} for r in fetched[: max_rows or None]]
        return QueryResult(
            source_id=self.source_id, rows=rows, columns=cols, executed_sql=display,
            params=[to_jsonable(p) for p in params],
            elapsed_ms=int((time.perf_counter() - t0) * 1000), truncated=truncated,
        )

    async def run_spec(self, spec: QuerySpec) -> QueryResult:
        compiled = compile_spec(spec)
        return await asyncio.to_thread(self._execute, compiled.sql, compiled.params,
                                       compiled.display_sql, None)

    async def run_readonly_sql(self, sql: str, params: list | None = None,
                               max_rows: int = 500, purpose: str = "exploratory") -> QueryResult:
        sql = assert_read_only(sql)
        params = list(params or [])
        display = inline_values(sql, params) if "?" in sql else sql
        driver_sql = convert_placeholders(sql, self.dialect)
        return await asyncio.to_thread(self._execute, driver_sql, params, display, max_rows)

    async def run_reference_sql(self, sql: str, named: dict[str, Any], max_rows: int = 5000) -> QueryResult:
        """Reference SQL uses :name placeholders (see ReconTest)."""
        sql = assert_read_only(sql)
        driver_sql, params = bind_named(sql, named, self.dialect)
        qmark_sql, _ = bind_named(sql, named, "sqlite")
        return await asyncio.to_thread(self._execute, driver_sql, params,
                                       inline_values(qmark_sql, params), max_rows)

    async def describe(self, tables: list[str]) -> dict[str, list[dict]]:
        out = {}
        for t in tables:
            validate_identifier(t, "table")
            if self.dialect == "sqlite":
                res = await self.run_readonly_sql(f"SELECT name, type FROM pragma_table_info('{t}')", max_rows=1000)
                out[t] = [{"column": r["name"], "type": r["type"]} for r in res.rows]
            else:
                schema, _, name = t.rpartition(".")
                sql = ("SELECT column_name, data_type FROM information_schema.columns "
                       "WHERE table_name = ?" + (" AND table_schema = ?" if schema else ""))
                res = await self.run_readonly_sql(sql, [name] + ([schema] if schema else []), max_rows=2000)
                out[t] = [{"column": r.get("column_name") or r.get("COLUMN_NAME"),
                           "type": r.get("data_type") or r.get("DATA_TYPE")} for r in res.rows]
        return out
