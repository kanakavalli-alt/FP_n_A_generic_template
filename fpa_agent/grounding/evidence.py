"""
Evidence ledger — the backtracking half of grounding.

Every number the engine produces is recorded as an Evidence record BEFORE it
reaches the model: which semantic-layer definition (and version hash) was
used, which source, which period, the exact executed SQL with values, the
resulting values, and — for derived numbers and analyses — the evidence ids
of the inputs. That gives every figure in a final answer a chain:

    answer number -> evidence(analysis) -> evidence(metric_query) -> SQL -> source

The grounding guard decides whether a number is allowed; the ledger answers
"where exactly did this number come from?" (explain_lineage tool, turn audit
log, reconciliation reports).
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from typing import Any

from pydantic import BaseModel, Field

from fpa_agent.connectors.base import to_jsonable


class Evidence(BaseModel):
    evidence_id: str
    kind: str                      # metric_query | derived | analysis | exploratory | reference
    created_at: float = Field(default_factory=time.time)
    source_id: str | None = None
    metric_id: str | None = None
    analysis_id: str | None = None
    definition_hash: str | None = None
    definition_status: str | None = None
    period: dict[str, Any] | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    group_by: list[str] = Field(default_factory=list)
    executed_sql: str | None = None
    formula: str | None = None
    inputs: list[str] = Field(default_factory=list)      # upstream evidence ids
    values: dict[str, Any] = Field(default_factory=dict)  # headline numbers produced
    row_count: int | None = None
    exploratory: bool = False


def definition_hash(obj: BaseModel) -> str:
    payload = json.dumps(obj.model_dump(mode="json"), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def new_id(prefix: str = "ev") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class EvidenceLedger:
    """In-memory, per-request ledger. Tool wrappers copy the records into the
    ADK session state (key 'evidence:<id>') so they survive across turns and
    can be explained later."""

    def __init__(self):
        self._items: dict[str, Evidence] = {}
        self._lock = threading.Lock()

    def add(self, **kwargs) -> Evidence:
        kwargs.setdefault("evidence_id", new_id())
        kwargs["values"] = {k: to_jsonable(v) for k, v in (kwargs.get("values") or {}).items()}
        ev = Evidence(**kwargs)
        with self._lock:
            self._items[ev.evidence_id] = ev
        return ev

    def get(self, evidence_id: str) -> Evidence | None:
        return self._items.get(evidence_id)

    def all(self) -> list[Evidence]:
        return list(self._items.values())

    def lineage(self, evidence_id: str, lookup=None) -> list[Evidence]:
        """Depth-first chain from an evidence record down to its source queries."""
        lookup = lookup or self.get
        seen, out, stack = set(), [], [evidence_id]
        while stack:
            eid = stack.pop()
            if eid in seen:
                continue
            seen.add(eid)
            ev = lookup(eid)
            if ev is None:
                continue
            out.append(ev)
            stack.extend(reversed(ev.inputs))
        return out
