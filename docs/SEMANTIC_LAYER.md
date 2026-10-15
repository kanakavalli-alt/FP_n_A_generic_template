# Semantic layer reference

Eight tables (`sl_` prefix in BigQuery, one YAML file each locally). Nested values are JSON in
STRING columns in BigQuery and native YAML locally. DDL: `semantic_layer/bigquery/ddl.sql`.
Fully worked example: `semantic_layer/demo/`. Blank, commented: `semantic_layer/client_template/`.

## sl_client_profile (one row)
`client_id, name, industry, description, currency, locale, calendar{fiscal_year_start_month,
fiscal_year_label: end|start, close_lag_days, default_comparison, ytd_basis}, terminology_notes,
data_freshness_note, support_contact` — injected into every agent prompt.

## sl_sources
`source_id, display_name, dialect, description, connection, allowed_tables`.
`connection` holds env var **names** only: `{mode: mcp, url_env}` or
`{mode: local, driver, path_env|dsn_env|kwargs_env}`. The validator rejects secret-looking keys.

## sl_dimensions
`dimension_id, display_name, description, aliases, values_hint`. Metrics map dimension ids to
their own columns, so "by region" works across sources. Display names/aliases also tell the
grounding guard which answer tables are dimensional breakdowns.

## sl_metrics
| Field | Notes |
|---|---|
| `metric_id`, `term`, `aliases`, `definition`, `business_context` | language the agent uses |
| `unit` (count/currency/percent/ratio/number), `decimals`, `favorable_direction` | presentation + favorability |
| `category` | `identity` (a number) or `causal` (discussed, never quantified) |
| `metric_type` | `base` (compiled SQL) or `derived` (formula) |
| `time_semantics` | `flow` (sum over period) or `snapshot` (level at last month) |
| `additive` | false for distinct counts / ratios — breakdowns then name the overlap |
| `source_id`, `table`, `table_alias`, `joins` | base: where (BigQuery column `table_name`) |
| `measure` | base: aggregate expression, e.g. `SUM(CASE WHEN x='A' THEN amt END)` |
| `filters` | `[{column, operator, values}]`, operators `= != <> > < >= <= IN NOT IN LIKE IS NULL IS NOT NULL BETWEEN` |
| `time` | `{pattern: date|year_month|period_key|fiscal_year|none, column, year_column, month_column, key_format}` |
| `dimensions` | `{dimension_id: column expression}` |
| `formula` | derived: `{a} / {b} * 100`, `abs()`, `min()`, `max()` |
| `budget_metric_id` | plan counterpart for `comparison=budget` |
| `owner`, `definition_status`, `open_questions` | governance gate (below) |
| `reference_sql` | client's own SQL, used only for reconciliation |
| `version`, `tags` | bookkeeping |

**Governance gate**

| status | agent behaviour |
|---|---|
| confirmed | answers |
| partial | answers + one plain caveat with `open_questions` and owner |
| incomplete | declines: definition/source not confirmed; names owner |
| blocked | declines: no connected source; names owner |
| category = causal | discusses qualitatively, never a number |

## sl_analyses
`analysis_id, title, description, question_examples, headline_metric, method, inputs{role:
metric_id}, dimension, default_comparison, params, status (live|partial|roadmap), owner, notes`.

| method | roles | dimension | params |
|---|---|---|---|
| delta | value | – | labels |
| contribution | value | required | labels |
| price_volume | value, volume | – | labels |
| price_volume_mix | value, volume | required | labels, excluded_categories, rate_is_ratio |
| ratio_bridge | numerator, denominator | – | labels |
| rollforward_bridge | level (snapshot), flows | – (`params.flow_dimension`) | labels, flow_dimension |

Budget comparison: the comparison side uses `inputs.budget_<role>` or the input metric's
`budget_metric_id`. `params.adjustments: [{label, metric_id, sign}]` appends post-netting lines
(e.g. a timing correction) fetched live for the current period. `notes` is internal and never
shown to users.

## sl_glossary
`term, aliases, definition, category, owner, related_metrics` — quotable definitions.

## sl_agent_profiles
`agent_id (fpa_analyst | data_explorer), display_name, role, persona, tone, response_format,
rules[], example_answers[], escalation_contact`. Shapes voice and format; grounding rules are
code-owned and appended after the persona, so a profile cannot relax them.

## sl_reconciliation_tests
`test_id, description, kind (metric|analysis|question), metric_id | analysis_id, period{...},
comparison, filters{dim:[values]}, group_by, reference_sql, reference_source_id, expected_value,
tolerance_abs, tolerance_pct, question, expected_tool, tags`.

Reference precedence: test `reference_sql` → test `expected_value` → metric `reference_sql`.
Named parameters: `:start_date :end_date (exclusive) :last_date :start_yyyymm :end_yyyymm
:start_ym :end_ym :fiscal_year :year`, and the `cmp_` versions for an analysis' comparison
period. Grouped tests return the dimension columns plus `value`; analysis tests return the
headline change as `value`.

## Period spec (tools and tests)
`{type: month|quarter|year|ytd|qtd|range|last_n_months, year, quarter, month, offset, fiscal,
start_year, start_month, end_year, end_month, start_date, end_date, n}` — resolved by
`fpa_agent/periods.py` using the client calendar.
