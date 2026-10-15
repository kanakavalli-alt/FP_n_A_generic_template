"""BigQuery schema of the semantic layer - single definition used to generate
the DDL (semantic_layer/bigquery/ddl.sql) and to load rows. Nested config is
stored as JSON in STRING columns so analysts can edit rows in the BigQuery UI
or a sheet without schema changes."""
from __future__ import annotations

S, I, F, B, T = "STRING", "INT64", "FLOAT64", "BOOL", "TIMESTAMP"

TABLE_SCHEMAS: dict[str, list[tuple[str, str, str]]] = {
    "client_profile": [
        ("client_id", S, "Short id of the client"),
        ("name", S, "Display name"),
        ("industry", S, "Industry / business model"),
        ("description", S, "What the business does - fed to every agent prompt"),
        ("currency", S, "Reporting currency, e.g. USD"),
        ("locale", S, "e.g. en-US"),
        ("calendar", S, "JSON: fiscal_year_start_month, fiscal_year_label, close_lag_days, default_comparison, ytd_basis"),
        ("terminology_notes", S, "How the business talks about itself"),
        ("data_freshness_note", S, "Latency / close rules users should know"),
        ("support_contact", S, "Escalation contact"),
    ],
    "sources": [
        ("source_id", S, "Id referenced by metrics"),
        ("display_name", S, "Human name of the system"),
        ("dialect", S, "sqlite|mssql|postgres|redshift|oracle|snowflake|bigquery|mysql"),
        ("description", S, "What lives in this system"),
        ("connection", S, "JSON: {mode: mcp, url_env} or {mode: local, driver, ..._env} - env var NAMES only"),
        ("allowed_tables", S, "Comma-separated allow-list for queries"),
    ],
    "dimensions": [
        ("dimension_id", S, "Id used in metrics.dimensions and analyses"),
        ("display_name", S, "Business name"),
        ("description", S, "Meaning"),
        ("aliases", S, "Comma-separated synonyms"),
        ("values_hint", S, "Comma-separated example values"),
    ],
    "metrics": [
        ("metric_id", S, "Stable id"),
        ("term", S, "Business name"),
        ("aliases", S, "Comma-separated synonyms users say"),
        ("definition", S, "Plain-English definition (quoted by the agent)"),
        ("business_context", S, "Why it matters / how it is used"),
        ("unit", S, "count|currency|percent|ratio|number"),
        ("decimals", I, "Display decimals"),
        ("favorable_direction", S, "higher|lower|neutral"),
        ("category", S, "identity (a number) | causal (qualitative only)"),
        ("metric_type", S, "base | derived"),
        ("time_semantics", S, "flow | snapshot"),
        ("additive", B, "Sums across dimension categories?"),
        ("source_id", S, "Base: source system"),
        ("table_name", S, "Base: table (loaded as `table`)"),
        ("table_alias", S, "Base: optional alias when joins are used"),
        ("joins", S, "JSON list: [{table, alias, on: [[l, r]], type}]"),
        ("measure", S, "Base: SQL aggregate expression, e.g. SUM(net_amount)"),
        ("filters", S, "JSON list: [{column, operator, values}]"),
        ("time", S, "JSON: {pattern: date|year_month|period_key|fiscal_year|none, column, year_column, month_column, key_format}"),
        ("dimensions", S, "JSON object: {dimension_id: column_expression}"),
        ("formula", S, "Derived: e.g. {net_revenue} / {units_sold}"),
        ("budget_metric_id", S, "Plan/budget counterpart metric"),
        ("owner", S, "Accountable owner"),
        ("definition_status", S, "confirmed|partial|incomplete|blocked"),
        ("open_questions", S, "What is unresolved (shown to users for partial/incomplete/blocked)"),
        ("reference_sql", S, "Client's own SQL for reconciliation (:start_date, :end_date, ...)"),
        ("version", S, "Definition version"),
        ("tags", S, "Comma-separated"),
    ],
    "analyses": [
        ("analysis_id", S, "Stable id"),
        ("title", S, "Business title"),
        ("description", S, "What it explains"),
        ("question_examples", S, "Comma- or newline-separated example questions"),
        ("headline_metric", S, "Metric whose change is explained"),
        ("method", S, "delta|contribution|price_volume|price_volume_mix|ratio_bridge|rollforward_bridge"),
        ("inputs", S, "JSON object: role -> metric_id (budget_<role> for budget side)"),
        ("dimension", S, "Dimension for dimension-based methods"),
        ("default_comparison", S, "prior_year|prior_period|budget"),
        ("params", S, "JSON: labels, excluded_categories, rate_is_ratio, flow_dimension, adjustments"),
        ("status", S, "live|partial|roadmap"),
        ("owner", S, "Accountable owner"),
        ("notes", S, "Internal notes (never shown to users)"),
    ],
    "glossary": [
        ("term", S, "Glossary term"),
        ("aliases", S, "Comma-separated"),
        ("definition", S, "Definition"),
        ("category", S, "Grouping"),
        ("owner", S, "Owner"),
        ("related_metrics", S, "Comma-separated metric ids"),
    ],
    "agent_profiles": [
        ("agent_id", S, "fpa_analyst | data_explorer"),
        ("display_name", S, "Name the agent introduces itself with"),
        ("role", S, "Role statement"),
        ("persona", S, "Persona / behaviour"),
        ("tone", S, "Tone of voice"),
        ("response_format", S, "Answer structure"),
        ("rules", S, "JSON list or newline-separated house rules"),
        ("example_answers", S, "JSON list of example answers"),
        ("escalation_contact", S, "Who to contact"),
    ],
    "reconciliation_tests": [
        ("test_id", S, "Stable id"),
        ("description", S, "What is being checked"),
        ("kind", S, "metric|analysis|question"),
        ("metric_id", S, ""), ("analysis_id", S, ""),
        ("period", S, "JSON period spec"),
        ("comparison", S, "prior_year|prior_period|budget"),
        ("filters", S, "JSON: {dimension: [values]}"),
        ("group_by", S, "Comma-separated dimension ids"),
        ("reference_sql", S, "Client's SQL (named params); grouped tests return dims + value"),
        ("reference_source_id", S, "Defaults to the metric's source"),
        ("expected_value", F, "Literal from the client's report"),
        ("tolerance_abs", F, ""), ("tolerance_pct", F, ""),
        ("question", S, "Natural-language question (kind=question)"),
        ("expected_tool", S, "Tool the agent should call"),
        ("tags", S, "Comma-separated"),
    ],
}

# YAML/model field name -> BigQuery column name, where they differ
RENAMES = {"metrics": {"table": "table_name"}}


def ddl(project: str = "{project}", dataset: str = "{dataset}", prefix: str = "sl_") -> str:
    out = [f"-- Semantic layer for the FP&A agent template. Generated from fpa_agent/semantic/schema.py",
           f"CREATE SCHEMA IF NOT EXISTS `{project}.{dataset}`;", ""]
    for table, cols in TABLE_SCHEMAS.items():
        body = ",\n".join(
            f'  {c} {t} OPTIONS(description="{desc.replace(chr(34), chr(39))}")' if desc else f"  {c} {t}"
            for c, t, desc in cols)
        out.append(f"CREATE TABLE IF NOT EXISTS `{project}.{dataset}.{prefix}{table}` (\n{body},\n"
                   f"  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP(),\n  updated_by STRING\n);\n")
    out.append(f"""CREATE TABLE IF NOT EXISTS `{project}.{dataset}.{prefix}change_log` (
  changed_at TIMESTAMP, changed_by STRING, catalog_version STRING, tables STRING, summary STRING
);""")
    return "\n".join(out)
