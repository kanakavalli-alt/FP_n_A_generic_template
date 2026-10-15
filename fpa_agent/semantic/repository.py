"""
Semantic-layer repository: loads the catalog from BigQuery (production) or a
directory of YAML files (local development, tests, CI).

Both backends produce the same immutable `Catalog`. The catalog is cached with
a short TTL (default 10 s): long enough that the many model calls inside one
turn share a single read, short enough that a catalog edit is live on the
next question with no redeploy. That matches the Kaplan behaviour where
`kaplan_use_cases` was read live.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from fpa_agent.semantic.schema import RENAMES
from fpa_agent.semantic.models import (
    AgentProfile, Analysis, ClientProfile, Dimension, GlossaryTerm, Metric,
    ReconTest, Source,
)

log = logging.getLogger(__name__)

# table name (without prefix) -> (model, key field)
TABLES: dict[str, tuple[type, str]] = {
    "client_profile": (ClientProfile, "client_id"),
    "sources": (Source, "source_id"),
    "dimensions": (Dimension, "dimension_id"),
    "metrics": (Metric, "metric_id"),
    "analyses": (Analysis, "analysis_id"),
    "glossary": (GlossaryTerm, "term"),
    "agent_profiles": (AgentProfile, "agent_id"),
    "reconciliation_tests": (ReconTest, "test_id"),
}


@dataclass(frozen=True)
class Catalog:
    profile: ClientProfile
    sources: dict[str, Source] = field(default_factory=dict)
    dimensions: dict[str, Dimension] = field(default_factory=dict)
    metrics: dict[str, Metric] = field(default_factory=dict)
    analyses: dict[str, Analysis] = field(default_factory=dict)
    glossary: dict[str, GlossaryTerm] = field(default_factory=dict)
    agent_profiles: dict[str, AgentProfile] = field(default_factory=dict)
    recon_tests: dict[str, ReconTest] = field(default_factory=dict)
    version: str = ""

    # ── lookups that raise a clear error instead of a bare KeyError ────────
    def metric(self, metric_id: str) -> Metric:
        try:
            return self.metrics[metric_id]
        except KeyError:
            raise KeyError(f"unknown metric_id {metric_id!r}") from None

    def analysis(self, analysis_id: str) -> Analysis:
        try:
            return self.analyses[analysis_id]
        except KeyError:
            raise KeyError(f"unknown analysis_id {analysis_id!r}") from None

    def source(self, source_id: str) -> Source:
        try:
            return self.sources[source_id]
        except KeyError:
            raise KeyError(f"unknown source_id {source_id!r}") from None

    def agent_profile(self, agent_id: str) -> AgentProfile:
        return self.agent_profiles.get(agent_id) or AgentProfile(agent_id=agent_id)

    def dimensions_of(self, metric_id: str, _seen: tuple = ()) -> list[str]:
        """Dimensions a metric can be filtered/broken down by. A derived
        metric supports the dimensions ALL of its inputs support."""
        m = self.metrics.get(metric_id)
        if m is None or metric_id in _seen:
            return []
        if m.metric_type == "base":
            return sorted(m.dimensions)
        sets = [set(self.dimensions_of(d, _seen + (metric_id,))) for d in m.depends_on]
        return sorted(set.intersection(*sets)) if sets else []

    def analyses_for_metric(self, metric_id: str) -> list[Analysis]:
        return [a for a in self.analyses.values()
                if a.headline_metric == metric_id or metric_id in a.inputs.values()]


def _build_catalog(raw: dict[str, list[dict]]) -> Catalog:
    profiles = raw.get("client_profile") or []
    if not profiles:
        raise ValueError("semantic layer has no client_profile row")
    built: dict[str, dict[str, Any]] = {}
    for name, (model, key) in TABLES.items():
        if name == "client_profile":
            continue
        out: dict[str, Any] = {}
        for row in raw.get(name) or []:
            obj = model.model_validate(row)
            k = getattr(obj, key)
            if k in out:
                raise ValueError(f"duplicate {key}={k!r} in {name}")
            out[k] = obj
        built[name] = out
    digest = hashlib.sha256(
        json.dumps(raw, sort_keys=True, default=str).encode()
    ).hexdigest()[:12]
    return Catalog(
        profile=ClientProfile.model_validate(profiles[0]),
        sources=built["sources"],
        dimensions=built["dimensions"],
        metrics=built["metrics"],
        analyses=built["analyses"],
        glossary=built["glossary"],
        agent_profiles=built["agent_profiles"],
        recon_tests=built["reconciliation_tests"],
        version=digest,
    )


class SemanticRepository(ABC):
    @abstractmethod
    def load_raw(self) -> dict[str, list[dict]]:
        """Every table's rows as plain dicts, keyed by table name (no prefix)."""

    def load_catalog(self) -> Catalog:
        return _build_catalog(self.load_raw())


class YamlRepository(SemanticRepository):
    """One YAML file per table: <dir>/<table>.yaml containing a list of rows
    (client_profile.yaml may contain a single mapping)."""

    def __init__(self, directory: str | os.PathLike):
        self.directory = Path(directory)

    def load_raw(self) -> dict[str, list[dict]]:
        raw: dict[str, list[dict]] = {}
        for name in TABLES:
            path = self.directory / f"{name}.yaml"
            if not path.exists():
                raw[name] = []
                continue
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
            except yaml.YAMLError as e:
                raise ValueError(f"{path.name}: invalid YAML - {e}") from None
            raw[name] = [data] if isinstance(data, dict) else list(data)
        return raw


class BigQueryRepository(SemanticRepository):
    """Reads `<project>.<dataset>.<prefix><table>` for every table. Nested
    config is stored as JSON STRING columns and decoded by the models."""

    def __init__(self, project: str, dataset: str, prefix: str = "sl_"):
        self.project, self.dataset, self.prefix = project, dataset, prefix

    def table_ref(self, name: str) -> str:
        return f"`{self.project}.{self.dataset}.{self.prefix}{name}`"

    def load_raw(self) -> dict[str, list[dict]]:
        from google.cloud import bigquery  # imported lazily: optional dependency
        client = bigquery.Client(project=self.project)
        raw: dict[str, list[dict]] = {}
        for name in TABLES:
            rows = client.query(f"SELECT * FROM {self.table_ref(name)}").result()
            back = {bq: f for f, bq in RENAMES.get(name, {}).items()}
            raw[name] = [{back.get(k, k): v for k, v in dict(r).items()
                          if v is not None and k not in ("updated_at", "updated_by")} for r in rows]
        return raw


# ─────────────────────────────────────────────────────────────────────────────
# Process-wide, TTL-cached access
# ─────────────────────────────────────────────────────────────────────────────

_TTL_SECONDS = float(os.getenv("FPA_CATALOG_TTL_SECONDS", "10"))
_lock = threading.Lock()
_cached: tuple[float, Catalog] | None = None
_repo_override: SemanticRepository | None = None


def repository_from_env() -> SemanticRepository:
    from fpa_agent.config import settings
    s = settings()
    if s.semantic_backend == "bigquery":
        return BigQueryRepository(s.bq_project, s.bq_dataset, s.bq_table_prefix)
    return YamlRepository(s.semantic_yaml_dir)


def set_repository(repo: SemanticRepository | None) -> None:
    """Point the process at a specific repository (tests, CLIs)."""
    global _repo_override, _cached
    with _lock:
        _repo_override, _cached = repo, None


def get_catalog(force_reload: bool = False) -> Catalog:
    """The current catalog. On a reload failure the last good catalog is kept
    (a transient BigQuery error must not take the agent down mid-session)."""
    global _cached
    now = time.monotonic()
    if not force_reload and _cached and now - _cached[0] < _TTL_SECONDS:
        return _cached[1]
    with _lock:
        if not force_reload and _cached and time.monotonic() - _cached[0] < _TTL_SECONDS:
            return _cached[1]
        repo = _repo_override or repository_from_env()
        try:
            catalog = repo.load_catalog()
        except Exception:
            if _cached:
                log.exception("semantic layer reload failed; serving last good catalog")
                _cached = (time.monotonic(), _cached[1])
                return _cached[1]
            raise
        _cached = (time.monotonic(), catalog)
        return catalog
