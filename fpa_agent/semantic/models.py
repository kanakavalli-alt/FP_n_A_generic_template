"""
Typed models for the semantic layer — the ONE source of truth the agents use.

Every table in the BigQuery semantic layer (see semantic_layer/bigquery/ddl.sql)
maps 1:1 to a model here. Nothing client-specific lives in Python: a new
client is onboarded by filling these tables, not by writing code.

    sl_client_profile        -> ClientProfile (+ Calendar)
    sl_sources               -> Source
    sl_dimensions            -> Dimension
    sl_metrics               -> Metric        (base AND derived metrics)
    sl_analyses              -> Analysis      (what used to be "use cases")
    sl_glossary              -> GlossaryTerm
    sl_agent_profiles        -> AgentProfile  (persona per agent)
    sl_reconciliation_tests  -> ReconTest     (source-match test cases)
"""
from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

DefinitionStatus = Literal["confirmed", "partial", "incomplete", "blocked"]
FavorableDirection = Literal["higher", "lower", "neutral"]
Unit = Literal["count", "currency", "percent", "ratio", "number"]
TimePattern = Literal["date", "year_month", "period_key", "fiscal_year", "none"]
TimeSemantics = Literal["flow", "snapshot"]

_FORMULA_REF_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _coerce_json(v: Any) -> Any:
    """BigQuery stores nested config as JSON-encoded STRING columns; YAML
    stores them natively. Accept both so one model serves both backends."""
    if isinstance(v, str):
        s = v.strip()
        if s.startswith(("{", "[")):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return v
    return v


def _coerce_list(v: Any) -> list:
    v = _coerce_json(v)
    if v is None or v == "":
        return []
    if isinstance(v, str):
        return [s.strip() for s in v.split(",") if s.strip()]
    return list(v)


class _Model(BaseModel):
    model_config = {"extra": "allow", "populate_by_name": True}


# ─────────────────────────────────────────────────────────────────────────────
# Client profile & calendar
# ─────────────────────────────────────────────────────────────────────────────

class Calendar(_Model):
    fiscal_year_start_month: int = Field(1, ge=1, le=12)
    # "end": FY2026 = the fiscal year that ENDS in calendar 2026 (most common).
    # "start": FY2026 = the fiscal year that STARTS in calendar 2026.
    fiscal_year_label: Literal["end", "start"] = "end"
    # A month counts as "closed" (safe to report) this many days after it ends.
    close_lag_days: int = 0
    default_comparison: Literal["prior_year", "prior_period", "budget"] = "prior_year"
    # YTD runs from the start of the fiscal or the calendar year.
    ytd_basis: Literal["fiscal", "calendar"] = "fiscal"


class ClientProfile(_Model):
    client_id: str
    name: str
    industry: str = ""
    description: str = ""
    currency: str = "USD"
    locale: str = "en-US"
    calendar: Calendar = Field(default_factory=Calendar)
    terminology_notes: str = ""         # how the client talks about its business
    data_freshness_note: str = ""
    support_contact: str = ""

    @field_validator("calendar", mode="before")
    @classmethod
    def _cal(cls, v):
        return _coerce_json(v) or {}


# ─────────────────────────────────────────────────────────────────────────────
# Sources & dimensions
# ─────────────────────────────────────────────────────────────────────────────

class Source(_Model):
    source_id: str
    display_name: str = ""
    dialect: Literal["sqlite", "mssql", "postgres", "redshift", "oracle",
                     "snowflake", "bigquery", "mysql"]
    description: str = ""
    # {"mode": "local", "driver": "sqlite", "path_env": "X"} or
    # {"mode": "mcp", "url_env": "ERP_MCP_URL"} — secrets never live here.
    connection: dict = Field(default_factory=dict)
    allowed_tables: list[str] = Field(default_factory=list)

    @field_validator("connection", mode="before")
    @classmethod
    def _conn(cls, v):
        return _coerce_json(v) or {}

    @field_validator("allowed_tables", mode="before")
    @classmethod
    def _tables(cls, v):
        return _coerce_list(v)


class Dimension(_Model):
    dimension_id: str
    display_name: str = ""
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    values_hint: list[str] = Field(default_factory=list)

    @field_validator("aliases", "values_hint", mode="before")
    @classmethod
    def _lists(cls, v):
        return _coerce_list(v)


# ─────────────────────────────────────────────────────────────────────────────
# Metrics
# ─────────────────────────────────────────────────────────────────────────────

class Filter(_Model):
    column: str
    operator: str = "="
    values: list[Any] = Field(default_factory=list)

    @field_validator("values", mode="before")
    @classmethod
    def _vals(cls, v):
        if v is None:
            return []
        return v if isinstance(v, list) else [v]


class Join(_Model):
    table: str
    alias: str
    on: list[list[str]]                 # [[left_col, right_col], ...]
    type: Literal["inner", "left"] = "inner"


class TimeBinding(_Model):
    pattern: TimePattern = "none"
    column: str | None = None           # date / period_key / fiscal_year column
    year_column: str | None = None      # year_month pattern
    month_column: str | None = None     # year_month pattern
    # period_key pattern: how the key is encoded
    key_format: Literal["YYYYMM", "YYYY_MM", "YYYY-MM"] = "YYYYMM"


class Metric(_Model):
    metric_id: str
    term: str
    aliases: list[str] = Field(default_factory=list)
    definition: str = ""
    business_context: str = ""
    unit: Unit = "number"
    decimals: int = 0
    favorable_direction: FavorableDirection = "higher"
    # identity: a number the source returns; causal: narrative only (no SQL)
    category: Literal["identity", "causal"] = "identity"
    metric_type: Literal["base", "derived"] = "base"
    time_semantics: TimeSemantics = "flow"
    # additive across dimensions (SUM) vs not (COUNT DISTINCT, ratios) — the
    # contribution analysis uses this to name the non-additive overlap.
    additive: bool = True

    # ── base metric: compiled to SQL by query/compiler.py ────────────────────
    source_id: str | None = None
    table: str | None = None
    table_alias: str | None = None
    joins: list[Join] = Field(default_factory=list)
    measure: str | None = None          # SQL aggregate expression, e.g. SUM(net_amount)
    filters: list[Filter] = Field(default_factory=list)
    time: TimeBinding = Field(default_factory=TimeBinding)
    dimensions: dict[str, str] = Field(default_factory=dict)  # dimension_id -> column expr

    # ── derived metric: evaluated in Decimal by engine/formulas.py ───────────
    formula: str | None = None          # e.g. "{net_revenue} / {active_students}"

    # The plan/budget counterpart of this metric (comparison="budget").
    budget_metric_id: str | None = None

    # ── governance ───────────────────────────────────────────────────────────
    owner: str = ""
    definition_status: DefinitionStatus = "incomplete"
    open_questions: str = ""
    # The client's OWN SQL for this metric, as run in their source system. Used
    # by the reconciliation module as the independent reference. Placeholders:
    # :start_date :end_date (exclusive) :start_yyyymm :end_yyyymm :fiscal_year
    reference_sql: str | None = None
    version: str = "1"
    tags: list[str] = Field(default_factory=list)

    @field_validator("aliases", "tags", mode="before")
    @classmethod
    def _lists(cls, v):
        return _coerce_list(v)

    @field_validator("joins", "filters", mode="before")
    @classmethod
    def _json_lists(cls, v):
        v = _coerce_json(v)
        return v or []

    @field_validator("time", "dimensions", mode="before")
    @classmethod
    def _json_dicts(cls, v):
        return _coerce_json(v) or {}

    @model_validator(mode="after")
    def _shape(self):
        if self.metric_type == "derived" and not self.formula:
            raise ValueError(f"derived metric {self.metric_id!r} needs a formula")
        return self

    @property
    def depends_on(self) -> list[str]:
        if not self.formula:
            return []
        return list(dict.fromkeys(_FORMULA_REF_RE.findall(self.formula)))

    @property
    def is_queryable(self) -> bool:
        return self.category == "identity" and self.definition_status in ("confirmed", "partial")


# ─────────────────────────────────────────────────────────────────────────────
# Analyses (generic replacement for client "use cases")
# ─────────────────────────────────────────────────────────────────────────────

class Analysis(_Model):
    analysis_id: str
    title: str
    description: str = ""
    question_examples: list[str] = Field(default_factory=list)
    headline_metric: str
    # A registered method in engine/methods.py, e.g. delta, contribution,
    # price_volume, price_volume_mix, ratio_bridge, rollforward_bridge.
    method: str
    # role -> metric_id. Roles required depend on the method. Budget comparison
    # uses "budget_<role>" bindings for the comparison side.
    inputs: dict[str, str] = Field(default_factory=dict)
    dimension: str | None = None
    default_comparison: Literal["prior_year", "prior_period", "budget"] | None = None
    # method parameters: labels, excluded_categories, rate_is_ratio,
    # flow_dimension, adjustments, ...
    params: dict[str, Any] = Field(default_factory=dict)
    status: Literal["live", "partial", "roadmap"] = "roadmap"
    owner: str = ""
    notes: str = ""

    @field_validator("question_examples", mode="before")
    @classmethod
    def _lists(cls, v):
        return _coerce_list(v)

    @field_validator("inputs", "params", mode="before")
    @classmethod
    def _json(cls, v):
        return _coerce_json(v) or {}


# ─────────────────────────────────────────────────────────────────────────────
# Knowledge base, personas, tests
# ─────────────────────────────────────────────────────────────────────────────

class GlossaryTerm(_Model):
    term: str
    aliases: list[str] = Field(default_factory=list)
    definition: str
    category: str = ""
    owner: str = ""
    related_metrics: list[str] = Field(default_factory=list)

    @field_validator("aliases", "related_metrics", mode="before")
    @classmethod
    def _lists(cls, v):
        return _coerce_list(v)


class AgentProfile(_Model):
    agent_id: str                       # fpa_analyst | data_explorer | ...
    display_name: str = ""
    role: str = ""
    persona: str = ""
    tone: str = ""
    response_format: str = ""
    rules: list[str] = Field(default_factory=list)
    example_answers: list[str] = Field(default_factory=list)
    escalation_contact: str = ""

    @field_validator("rules", "example_answers", mode="before")
    @classmethod
    def _lists(cls, v):
        v = _coerce_json(v)
        if isinstance(v, str):
            return [s.strip() for s in v.split("\n") if s.strip()]
        return v or []


class ReconTest(_Model):
    test_id: str
    description: str = ""
    kind: Literal["metric", "analysis", "question"] = "metric"
    metric_id: str | None = None
    analysis_id: str | None = None
    period: dict[str, Any] = Field(default_factory=dict)       # periods.PeriodSpec
    comparison: str | None = None
    filters: dict[str, list[Any]] = Field(default_factory=dict)
    group_by: list[str] = Field(default_factory=list)
    # Independent reference — the client's own source-system SQL...
    reference_sql: str | None = None
    reference_source_id: str | None = None
    # ...and/or a literal figure from the client's own report.
    expected_value: float | None = None
    tolerance_abs: float = 0.0
    tolerance_pct: float = 0.0
    question: str | None = None         # natural-language question (kind=question)
    expected_tool: str | None = None
    tags: list[str] = Field(default_factory=list)

    @field_validator("period", "filters", mode="before")
    @classmethod
    def _json(cls, v):
        return _coerce_json(v) or {}

    @field_validator("group_by", "tags", mode="before")
    @classmethod
    def _lists(cls, v):
        return _coerce_list(v)
