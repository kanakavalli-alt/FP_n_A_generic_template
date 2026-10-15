"""
Connector for the generic MCP server (mcp_server/server.py) deployed next to
each source system.

Called as a plain authenticated MCP client from an async Python function, NOT
through ADK's MCPToolset/AgentTool. The Kaplan build learned the hard way that
nested agent-to-agent MCP calls tear down and rebuild the session every hop
(65-230 s per question); a direct client call pays for one round trip.

Auth: a Google-signed OIDC ID token for the agent's service account, audience
= the server URL (the server verifies signature, audience and caller email).
Key material: AGENT_SA_KEY_JSON, then AGENT_SA_KEY_PATH, then Secret Manager
(AGENT_SA_KEY_SECRET) — never in the semantic layer.
"""
from __future__ import annotations

import asyncio
import json
import os
import weakref
from typing import Any

from fpa_agent.connectors.base import Connector, DataSourceUnavailableError, QueryResult
from fpa_agent.query.compiler import QuerySpec

_creds: dict[str, Any] = {}
_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()


def _loop_lock() -> asyncio.Lock:
    # One lock per event loop: Agent Engine runs requests on fresh loops inside
    # a warm process, and an asyncio.Lock is bound to the loop that created it.
    loop = asyncio.get_running_loop()
    if loop not in _locks:
        _locks[loop] = asyncio.Lock()
    return _locks[loop]


async def _sa_key_json() -> str | None:
    if os.getenv("AGENT_SA_KEY_JSON"):
        return os.environ["AGENT_SA_KEY_JSON"]
    secret = os.getenv("AGENT_SA_KEY_SECRET")
    if secret and not os.getenv("AGENT_SA_KEY_PATH"):
        def _fetch():
            from google.cloud import secretmanager
            client = secretmanager.SecretManagerServiceClient()
            return client.access_secret_version(name=secret).payload.data.decode()
        return await asyncio.to_thread(_fetch)
    return None


async def bearer_header(audience: str) -> dict[str, str]:
    key_json = await _sa_key_json()
    key_path = os.getenv("AGENT_SA_KEY_PATH")
    if not key_json and not key_path:
        return {}
    async with _loop_lock():
        creds = _creds.get(audience)
        if creds is None:
            from google.oauth2 import service_account
            if key_json:
                creds = service_account.IDTokenCredentials.from_service_account_info(
                    json.loads(key_json), target_audience=audience)
            else:
                creds = service_account.IDTokenCredentials.from_service_account_file(
                    key_path, target_audience=audience)
            _creds[audience] = creds
        if not creds.valid:
            import google.auth.transport.requests as req
            await asyncio.to_thread(creds.refresh, req.Request())
    return {"Authorization": f"Bearer {creds.token}"}


class McpConnector(Connector):
    def __init__(self, source_id: str, url: str, attempts: int = 2, timeout: int = 60):
        self.source_id = source_id
        self.url = url
        self.attempts = attempts
        self.timeout = timeout

    @classmethod
    def from_config(cls, source_id: str, cfg: dict) -> "McpConnector":
        url = os.getenv(cfg.get("url_env", ""), "") or cfg.get("url", "")
        if not url:
            raise ValueError(f"source {source_id}: MCP url not configured (set {cfg.get('url_env')})")
        return cls(source_id, url)

    async def _call_once(self, tool: str, args: dict) -> dict:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        headers = await bearer_header(self.url)
        async with streamablehttp_client(self.url, headers=headers, timeout=30,
                                         sse_read_timeout=self.timeout) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, args)
                text = result.content[0].text if result.content else "{}"
                return json.loads(text)

    async def _call(self, tool: str, args: dict) -> dict:
        last: Exception | None = None
        for attempt in range(self.attempts):
            try:
                payload = await self._call_once(tool, args)
                break
            except Exception as exc:  # transport failure: retry once, then give up honestly
                last = exc
                await asyncio.sleep(1.0 * (attempt + 1))
        else:
            raise DataSourceUnavailableError(f"{self.source_id} unreachable: {last}") from last
        if payload.get("status") != "success":
            raise RuntimeError(f"{self.source_id}: {payload.get('error', 'query failed')}")
        return payload

    def _result(self, payload: dict) -> QueryResult:
        return QueryResult(
            source_id=self.source_id,
            rows=payload.get("rows", []),
            columns=payload.get("columns", []),
            executed_sql=payload.get("executed_sql", ""),
            params=payload.get("params", []),
            elapsed_ms=payload.get("elapsed_ms", 0),
            truncated=payload.get("truncated", False),
        )

    async def run_spec(self, spec: QuerySpec) -> QueryResult:
        return self._result(await self._call("run_query_spec", {"spec": spec.model_dump(mode="json")}))

    async def run_readonly_sql(self, sql: str, params: list | None = None,
                               max_rows: int = 500, purpose: str = "exploratory") -> QueryResult:
        return self._result(await self._call("run_readonly_sql", {
            "sql": sql, "params": params or [], "max_rows": max_rows, "purpose": purpose,
        }))

    async def run_reference_sql(self, sql: str, named: dict[str, Any], max_rows: int = 5000) -> QueryResult:
        return self._result(await self._call("run_reference_sql", {
            "sql": sql, "named_params": named, "max_rows": max_rows,
        }))

    async def describe(self, tables: list[str]) -> dict[str, list[dict]]:
        payload = await self._call("describe_schema", {"tables": tables})
        return payload.get("tables", {})
