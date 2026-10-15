"""Connector contract: how the engine reaches a source system.

Two implementations ship with the template:
  * LocalSqlConnector — direct DB-API connection (dev, tests, CI, or a
    deployment where the agent runtime can reach the database itself);
  * McpConnector      — calls the generic MCP server deployed next to the
    database (production pattern from the Kaplan build: OIDC-authenticated,
    read-only, credentials in Secret Manager on the server side).

Both take a structured QuerySpec for headline numbers; free-text SQL is only
accepted on the read-only exploratory/reference path.
"""
from __future__ import annotations

import datetime as _dt
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from fpa_agent.query.compiler import QuerySpec


class DataSourceUnavailableError(RuntimeError):
    """The source could not be reached at all (as opposed to answering with a
    real query error). Tools turn this into an honest "temporarily
    unavailable" answer instead of a crash or a guessed number."""


class QueryResult(BaseModel):
    source_id: str
    rows: list[dict[str, Any]] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    executed_sql: str = ""                 # SQL with values inlined (audit/display)
    params: list[Any] = Field(default_factory=list)
    elapsed_ms: int = 0
    truncated: bool = False


def to_jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (_dt.date, _dt.datetime)):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.hex()
    return v


class Connector(ABC):
    source_id: str

    @abstractmethod
    async def run_spec(self, spec: QuerySpec) -> QueryResult:
        """Compile + execute a structured metric query (headline path)."""

    @abstractmethod
    async def run_readonly_sql(self, sql: str, params: list | None = None,
                               max_rows: int = 500, purpose: str = "exploratory") -> QueryResult:
        """Execute a read-only SELECT (exploration / reconciliation reference)."""

    async def run_reference_sql(self, sql: str, named: dict[str, Any], max_rows: int = 5000) -> QueryResult:
        """Read-only client reference SQL with :name placeholders (reconciliation)."""
        raise NotImplementedError

    async def describe(self, tables: list[str]) -> dict[str, list[dict]]:
        raise NotImplementedError

    async def ping(self) -> bool:
        try:
            await self.run_readonly_sql("SELECT 1 AS ok", max_rows=1, purpose="ping")
            return True
        except Exception:
            return False
