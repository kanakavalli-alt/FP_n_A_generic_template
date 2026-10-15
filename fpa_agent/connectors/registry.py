"""source_id -> Connector, built from sl_sources.connection. Tests and the
reconciliation runner can inject connectors directly."""
from __future__ import annotations

import threading

from fpa_agent.connectors.base import Connector
from fpa_agent.semantic.repository import Catalog

_lock = threading.Lock()
_cache: dict[str, Connector] = {}
_overrides: dict[str, Connector] = {}


def set_connector(source_id: str, connector: Connector | None) -> None:
    with _lock:
        if connector is None:
            _overrides.pop(source_id, None)
        else:
            _overrides[source_id] = connector
        _cache.pop(source_id, None)


def clear() -> None:
    with _lock:
        _cache.clear()
        _overrides.clear()


def get_connector(catalog: Catalog, source_id: str) -> Connector:
    with _lock:
        if source_id in _overrides:
            return _overrides[source_id]
        if source_id in _cache:
            return _cache[source_id]
        src = catalog.source(source_id)
        mode = src.connection.get("mode", "local")
        if mode == "mcp":
            from fpa_agent.connectors.mcp_connector import McpConnector
            conn: Connector = McpConnector.from_config(source_id, src.connection)
        elif mode == "local":
            from fpa_agent.connectors.local_sql import LocalSqlConnector
            conn = LocalSqlConnector.from_config(source_id, src.dialect, src.connection)
        else:
            raise ValueError(f"source {source_id}: unknown connection mode {mode!r}")
        _cache[source_id] = conn
        return conn
