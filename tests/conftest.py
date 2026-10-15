"""Shared fixtures: the demo client's source databases (built fresh into a
temp dir) wired to the demo semantic layer, with a fixed 'today'."""
from __future__ import annotations

import asyncio
import importlib.util
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fpa_agent.connectors import registry  # noqa: E402
from fpa_agent.connectors.local_sql import LocalSqlConnector  # noqa: E402
from fpa_agent.semantic.repository import YamlRepository, set_repository  # noqa: E402

AS_OF = date(2026, 10, 15)
DEMO_DIR = ROOT / "semantic_layer" / "demo"


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_demo_db", ROOT / "demo" / "build_demo_db.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def demo_dbs(tmp_path_factory):
    out = tmp_path_factory.mktemp("demo_data")
    sis, erp = _load_builder().build(out)
    return {"sis": sis, "erp": erp}


@pytest.fixture(scope="session")
def catalog():
    return YamlRepository(DEMO_DIR).load_catalog()


@pytest.fixture(autouse=True)
def wired(demo_dbs, catalog):
    """Every test sees the demo catalog and connectors to the temp databases."""
    set_repository(YamlRepository(DEMO_DIR))
    for sid, path in demo_dbs.items():
        p = str(path).replace("\\", "/")
        registry.set_connector(sid, LocalSqlConnector(
            sid, "sqlite", lambda p=p: sqlite3.connect(f"file:{p}?mode=ro", uri=True, check_same_thread=False)))
    yield
    registry.clear()
    set_repository(None)


@pytest.fixture
def run():
    return lambda coro: asyncio.run(coro)


@pytest.fixture
def sql(demo_dbs):
    """Run raw SQL directly on a demo DB (an independent path for asserts)."""
    def _q(source: str, query: str, *params):
        with sqlite3.connect(demo_dbs[source]) as c:
            return c.execute(query, params).fetchall()
    return _q
