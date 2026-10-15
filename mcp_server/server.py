"""
Generic, read-only MCP server — ONE codebase for every source system.

Replaces the per-database servers of the Kaplan build (ms_sql_server.py,
redshift_server.py, salesforce_server.py, oracle probe) and their hand-written
`*_queries.py` files. Deploy one instance per source, next to the database
(VM / Windows service / Cloud Run with VPC connector), configured by env:

  SOURCE_ID            id of this source in the semantic layer (sl_sources)
  DIALECT              mssql | postgres | redshift | oracle | snowflake | mysql | sqlite | bigquery
  DB_DRIVER            pyodbc | redshift_connector | oracledb | psycopg2 | snowflake.connector | pymysql | sqlite
  DB_CONN_SECRET       Secret Manager version holding the DSN or JSON connect kwargs (production)
  DB_CONN              DSN / JSON kwargs / sqlite path (local development only)
  ALLOWED_TABLES       comma-separated allow-list (enforced on every query)
  MCP_EXPECTED_AUDIENCE      comma-separated accepted OIDC audiences (this server's URL(s))
  MCP_EXPECTED_CALLER_EMAIL  the agent service account allowed to call
  AUTH_DISABLED=true   local development only — never in a deployed environment
  MAX_ROWS (default 5000), PORT (default 8080)

Tools
  run_query_spec      headline path: a STRUCTURED QuerySpec (built by the agent's code from
                      the semantic layer) is re-validated and compiled HERE by the same
                      compiler; the server never executes SQL text it was handed for a
                      headline number.
  run_readonly_sql    exploration: one SELECT/WITH, write keywords rejected anywhere
                      (including DML hidden in CTEs), allow-listed tables, row cap.
  run_reference_sql   reconciliation: the client's own read-only SQL with :named params.
  describe_schema     columns for allow-listed tables.
  ping                health check.
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import re
import sqlite3
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # fpa_agent package
sys.path.insert(0, str(Path(__file__).resolve().parent))         # auth.py

try:  # MCP SDK 1.x (pinned in requirements; the line validated in production)
    from mcp.server.fastmcp import FastMCP  # noqa: E402
except ImportError:  # MCP SDK 2.x renamed FastMCP -> MCPServer (host/port moved to run())
    from mcp.server.mcpserver import MCPServer as _MCPServer  # noqa: E402

    def FastMCP(name, host=None, port=None):  # noqa: N802
        return _MCPServer(name)

from fpa_agent.connectors.local_sql import LocalSqlConnector  # noqa: E402
from fpa_agent.query.compiler import QueryCompileError, QuerySpec  # noqa: E402

# ── logging: stderr + rotating file (stdout is reserved for stdio MCP) ───────
_log_dir = Path(os.getenv("LOG_DIR", Path(__file__).resolve().parent / "logs"))
_log_dir.mkdir(parents=True, exist_ok=True)
log = logging.getLogger("mcp_source_server")
log.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
_fmt = logging.Formatter("%(asctime)s %(levelname)-5s [%(name)s] %(message)s")
for h in (logging.StreamHandler(sys.stderr),
          RotatingFileHandler(_log_dir / "server.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")):
    h.setFormatter(_fmt)
    log.addHandler(h)

SOURCE_ID = os.getenv("SOURCE_ID", "source")
DIALECT = os.getenv("DIALECT", "sqlite")
ALLOWED = {t.strip().lower() for t in os.getenv("ALLOWED_TABLES", "").split(",") if t.strip()}
MAX_ROWS = int(os.getenv("MAX_ROWS", "5000"))
_FROM_RE = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_.]*)", re.IGNORECASE)


def _conn_string() -> str:
    secret = os.getenv("DB_CONN_SECRET")
    if secret:
        from google.cloud import secretmanager
        return secretmanager.SecretManagerServiceClient().access_secret_version(
            name=secret).payload.data.decode()
    return os.getenv("DB_CONN", "")


def _connect_factory():
    driver = os.getenv("DB_DRIVER", "sqlite")
    raw = _conn_string()
    if driver == "sqlite":
        return lambda: sqlite3.connect(f"file:{raw}?mode=ro", uri=True, check_same_thread=False)
    module = importlib.import_module(driver)
    if raw.strip().startswith("{"):
        kwargs = json.loads(raw)
        return lambda: module.connect(**kwargs)
    return lambda: module.connect(raw)


_connector: LocalSqlConnector | None = None


def connector() -> LocalSqlConnector:
    global _connector
    if _connector is None:
        _connector = LocalSqlConnector(SOURCE_ID, DIALECT, _connect_factory())
    return _connector


def _check_tables(names) -> None:
    if not ALLOWED:
        return
    bad = sorted({n.lower() for n in names} - ALLOWED)
    if bad:
        raise QueryCompileError(f"table(s) {bad} are not on this server's allow-list")


def _ok(res, **extra) -> dict:
    return {"source": SOURCE_ID, "status": "success", "rows": res.rows[:MAX_ROWS], "columns": res.columns,
            "executed_sql": res.executed_sql, "params": res.params, "elapsed_ms": res.elapsed_ms,
            "truncated": res.truncated or len(res.rows) > MAX_ROWS, **extra}


def _err(e: Exception) -> dict:
    # Deterministic, user-safe error text; full detail stays in the server log.
    log.warning("query rejected/failed: %s", e)
    msg = str(e) if isinstance(e, (QueryCompileError, ValueError)) else f"{type(e).__name__} while querying {SOURCE_ID}"
    return {"source": SOURCE_ID, "status": "error", "error": msg, "rows": []}


mcp = FastMCP(f"{SOURCE_ID}_source", host=os.getenv("MCP_HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8080")))


@mcp.tool()
async def run_query_spec(spec: dict) -> dict:
    """Compile and run a structured metric query (headline numbers)."""
    try:
        qs = QuerySpec.model_validate(spec)
        if qs.dialect != DIALECT:
            raise QueryCompileError(f"spec dialect {qs.dialect} != server dialect {DIALECT}")
        _check_tables([qs.table, *[j.table for j in qs.joins]])
        res = await connector().run_spec(qs)
        log.info("run_query_spec ok rows=%d ms=%d", len(res.rows), res.elapsed_ms)
        return _ok(res)
    except Exception as e:
        return _err(e)


@mcp.tool()
async def run_readonly_sql(sql: str, params: list | None = None, max_rows: int = 500,
                           purpose: str = "exploratory") -> dict:
    """Exploratory read-only SELECT. Results are never headline numbers."""
    try:
        _check_tables(_FROM_RE.findall(sql))
        res = await connector().run_readonly_sql(sql, params, min(max_rows, MAX_ROWS), purpose)
        log.info("run_readonly_sql purpose=%s rows=%d", purpose, len(res.rows))
        return _ok(res, exploratory=True)
    except Exception as e:
        return _err(e)


@mcp.tool()
async def run_reference_sql(sql: str, named_params: dict | None = None, max_rows: int = 5000) -> dict:
    """Client reference SQL for reconciliation (:named parameters)."""
    try:
        _check_tables(_FROM_RE.findall(sql))
        res = await connector().run_reference_sql(sql, named_params or {}, min(max_rows, MAX_ROWS))
        return _ok(res, purpose="reconciliation")
    except Exception as e:
        return _err(e)


@mcp.tool()
async def describe_schema(tables: list[str]) -> dict:
    """Columns and types of allow-listed tables."""
    try:
        _check_tables(tables)
        return {"source": SOURCE_ID, "status": "success", "tables": await connector().describe(tables)}
    except Exception as e:
        return _err(e)


@mcp.tool()
async def ping() -> dict:
    """Health check: can this server reach its database?"""
    ok = await connector().ping()
    return {"source": SOURCE_ID, "status": "success" if ok else "error", "dialect": DIALECT}


def build_app():
    from auth import AgentIdentityMiddleware  # local module
    app = mcp.streamable_http_app()
    if os.getenv("AUTH_DISABLED", "").lower() == "true":
        log.warning("AUTH_DISABLED=true — accepting unauthenticated callers (development only)")
        return app
    return AgentIdentityMiddleware(app)


if __name__ == "__main__":
    import argparse
    import asyncio

    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--test-connection", action="store_true", help="ping the database and exit")
    a = ap.parse_args()
    if a.test_connection:
        ok = asyncio.run(connector().ping())
        print("connection OK" if ok else "connection FAILED")
        sys.exit(0 if ok else 1)
    uvicorn.run(build_app(), host=os.getenv("MCP_HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8080")))
