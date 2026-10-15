-- Semantic layer for the FP&A agent template. Generated from fpa_agent/semantic/schema.py
CREATE SCHEMA IF NOT EXISTS `YOUR_PROJECT.fpa_semantic_layer`;

CREATE TABLE IF NOT EXISTS `YOUR_PROJECT.fpa_semantic_layer.sl_client_profile` (
  client_id STRING OPTIONS(description="Short id of the client"),
  name STRING OPTIONS(description="Display name"),
  industry STRING OPTIONS(description="Industry / business model"),
  description STRING OPTIONS(description="What the business does - fed to every agent prompt"),
  currency STRING OPTIONS(description="Reporting currency, e.g. USD"),
  locale STRING OPTIONS(description="e.g. en-US"),
  calendar STRING OPTIONS(description="JSON: fiscal_year_start_month, fiscal_year_label, close_lag_days, default_comparison, ytd_basis"),
  terminology_notes STRING OPTIONS(description="How the business talks about itself"),
  data_freshness_note STRING OPTIONS(description="Latency / close rules users should know"),
  support_contact STRING OPTIONS(description="Escalation contact"),
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP(),
  updated_by STRING
);

CREATE TABLE IF NOT EXISTS `YOUR_PROJECT.fpa_semantic_layer.sl_sources` (
  source_id STRING OPTIONS(description="Id referenced by metrics"),
  display_name STRING OPTIONS(description="Human name of the system"),
  dialect STRING OPTIONS(description="sqlite|mssql|postgres|redshift|oracle|snowflake|bigquery|mysql"),
  description STRING OPTIONS(description="What lives in this system"),
  connection STRING OPTIONS(description="JSON: {mode: mcp, url_env} or {mode: local, driver, ..._env} - env var NAMES only"),
  allowed_tables STRING OPTIONS(description="Comma-separated allow-list for queries"),
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP(),
  updated_by STRING
);

CREATE TABLE IF NOT EXISTS `YOUR_PROJECT.fpa_semantic_layer.sl_dimensions` (
  dimension_id STRING OPTIONS(description="Id used in metrics.dimensions and analyses"),
  display_name STRING OPTIONS(description="Business name"),
  description STRING OPTIONS(description="Meaning"),
  aliases STRING OPTIONS(description="Comma-separated synonyms"),
  values_hint STRING OPTIONS(description="Comma-separated example values"),
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP(),
  updated_by STRING
);

CREATE TABLE IF NOT EXISTS `YOUR_PROJECT.fpa_semantic_layer.sl_metrics` (
  metric_id STRING OPTIONS(description="Stable id"),
  term STRING OPTIONS(description="Business name"),
  aliases STRING OPTIONS(description="Comma-separated synonyms users say"),
  definition STRING OPTIONS(description="Plain-English definition (quoted by the agent)"),
  business_context STRING OPTIONS(description="Why it matters / how it is used"),
  unit STRING OPTIONS(description="count|currency|percent|ratio|number"),
  decimals INT64 OPTIONS(description="Display decimals"),
  favorable_direction STRING OPTIONS(description="higher|lower|neutral"),
  category STRING OPTIONS(description="identity (a number) | causal (qualitative only)"),
  metric_type STRING OPTIONS(description="base | derived"),
  time_semantics STRING OPTIONS(description="flow | snapshot"),
  additive BOOL OPTIONS(description="Sums across dimension categories?"),
  source_id STRING OPTIONS(description="Base: source system"),
  table_name STRING OPTIONS(description="Base: table (loaded as `table`)"),
  table_alias STRING OPTIONS(description="Base: optional alias when joins are used"),
  joins STRING OPTIONS(description="JSON list: [{table, alias, on: [[l, r]], type}]"),
  measure STRING OPTIONS(description="Base: SQL aggregate expression, e.g. SUM(net_amount)"),
  filters STRING OPTIONS(description="JSON list: [{column, operator, values}]"),
  time STRING OPTIONS(description="JSON: {pattern: date|year_month|period_key|fiscal_year|none, column, year_column, month_column, key_format}"),
  dimensions STRING OPTIONS(description="JSON object: {dimension_id: column_expression}"),
  formula STRING OPTIONS(description="Derived: e.g. {net_revenue} / {units_sold}"),
  budget_metric_id STRING OPTIONS(description="Plan/budget counterpart metric"),
  owner STRING OPTIONS(description="Accountable owner"),
  definition_status STRING OPTIONS(description="confirmed|partial|incomplete|blocked"),
  open_questions STRING OPTIONS(description="What is unresolved (shown to users for partial/incomplete/blocked)"),
  reference_sql STRING OPTIONS(description="Client's own SQL for reconciliation (:start_date, :end_date, ...)"),
  version STRING OPTIONS(description="Definition version"),
  tags STRING OPTIONS(description="Comma-separated"),
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP(),
  updated_by STRING
);

CREATE TABLE IF NOT EXISTS `YOUR_PROJECT.fpa_semantic_layer.sl_analyses` (
  analysis_id STRING OPTIONS(description="Stable id"),
  title STRING OPTIONS(description="Business title"),
  description STRING OPTIONS(description="What it explains"),
  question_examples STRING OPTIONS(description="Comma- or newline-separated example questions"),
  headline_metric STRING OPTIONS(description="Metric whose change is explained"),
  method STRING OPTIONS(description="delta|contribution|price_volume|price_volume_mix|ratio_bridge|rollforward_bridge"),
  inputs STRING OPTIONS(description="JSON object: role -> metric_id (budget_<role> for budget side)"),
  dimension STRING OPTIONS(description="Dimension for dimension-based methods"),
  default_comparison STRING OPTIONS(description="prior_year|prior_period|budget"),
  params STRING OPTIONS(description="JSON: labels, excluded_categories, rate_is_ratio, flow_dimension, adjustments"),
  status STRING OPTIONS(description="live|partial|roadmap"),
  owner STRING OPTIONS(description="Accountable owner"),
  notes STRING OPTIONS(description="Internal notes (never shown to users)"),
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP(),
  updated_by STRING
);

CREATE TABLE IF NOT EXISTS `YOUR_PROJECT.fpa_semantic_layer.sl_glossary` (
  term STRING OPTIONS(description="Glossary term"),
  aliases STRING OPTIONS(description="Comma-separated"),
  definition STRING OPTIONS(description="Definition"),
  category STRING OPTIONS(description="Grouping"),
  owner STRING OPTIONS(description="Owner"),
  related_metrics STRING OPTIONS(description="Comma-separated metric ids"),
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP(),
  updated_by STRING
);

CREATE TABLE IF NOT EXISTS `YOUR_PROJECT.fpa_semantic_layer.sl_agent_profiles` (
  agent_id STRING OPTIONS(description="fpa_analyst | data_explorer"),
  display_name STRING OPTIONS(description="Name the agent introduces itself with"),
  role STRING OPTIONS(description="Role statement"),
  persona STRING OPTIONS(description="Persona / behaviour"),
  tone STRING OPTIONS(description="Tone of voice"),
  response_format STRING OPTIONS(description="Answer structure"),
  rules STRING OPTIONS(description="JSON list or newline-separated house rules"),
  example_answers STRING OPTIONS(description="JSON list of example answers"),
  escalation_contact STRING OPTIONS(description="Who to contact"),
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP(),
  updated_by STRING
);

CREATE TABLE IF NOT EXISTS `YOUR_PROJECT.fpa_semantic_layer.sl_reconciliation_tests` (
  test_id STRING OPTIONS(description="Stable id"),
  description STRING OPTIONS(description="What is being checked"),
  kind STRING OPTIONS(description="metric|analysis|question"),
  metric_id STRING,
  analysis_id STRING,
  period STRING OPTIONS(description="JSON period spec"),
  comparison STRING OPTIONS(description="prior_year|prior_period|budget"),
  filters STRING OPTIONS(description="JSON: {dimension: [values]}"),
  group_by STRING OPTIONS(description="Comma-separated dimension ids"),
  reference_sql STRING OPTIONS(description="Client's SQL (named params); grouped tests return dims + value"),
  reference_source_id STRING OPTIONS(description="Defaults to the metric's source"),
  expected_value FLOAT64 OPTIONS(description="Literal from the client's report"),
  tolerance_abs FLOAT64,
  tolerance_pct FLOAT64,
  question STRING OPTIONS(description="Natural-language question (kind=question)"),
  expected_tool STRING OPTIONS(description="Tool the agent should call"),
  tags STRING OPTIONS(description="Comma-separated"),
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP(),
  updated_by STRING
);

CREATE TABLE IF NOT EXISTS `YOUR_PROJECT.fpa_semantic_layer.sl_change_log` (
  changed_at TIMESTAMP, changed_by STRING, catalog_version STRING, tables STRING, summary STRING
);
