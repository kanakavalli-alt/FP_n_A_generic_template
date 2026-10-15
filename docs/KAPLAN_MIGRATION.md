# Mapping the Kaplan build onto the template

How every Kaplan component and use case is expressed in the generic template. Kaplan itself
could be re-platformed onto this template with catalogue rows only.

## Components

| Kaplan | Template |
|---|---|
| `kaplan_metrics_new` (term, aliases, table_name, field_name, filters, period_filter_pattern, definition_status, owner, reference_sql …) | `sl_metrics` (same governance fields; `measure` + structured `time` replace field_name + period_filter_pattern) |
| `kaplan_use_cases` (variance_method / recipe JSON, fixed_query_keys) | `sl_analyses` (`method` + `inputs` role bindings; no query keys — SQL is compiled) |
| `kaplan_config` (category_type_map, filter_op_template, period_pattern_template) | built into the compiler/models (`category`, operator whitelist, time patterns) |
| Vertex AI Search glossary | `sl_glossary` (+ keep Vertex AI Search for long documents if needed) |
| `rag_tool.lookup_fp_and_a_term` + status gate | `search_semantic_layer` / `get_definition` + `MetricService.gate_metric` |
| `use_case_tool`, `agent_routing_tool` | not needed — one hop, sources resolved from the metric row |
| `uc_dedicated_tools.py` (UC1–UC4 single-call tools) | `get_metric`, `compare_metric`, `run_analysis` |
| `mcp_servers/*_queries.py` fixed SQL | compiled from `sl_metrics`; client SQL kept as `reference_sql` |
| `query_builder.py` (aggregate / aggregate_by_group) | `query/compiler.py` (adds joins, snapshots, monthly grain, 8 dialects) |
| `tools/variance/*` (pvm, mix_rate, flow_base, resolver, fetch_plan, timing) | `engine/primitives.py` + `engine/methods.py` + `periods.py` + `params.adjustments` |
| `calculator_tool` | `calculate` (refuses inputs not returned by a tool this turn) |
| `period_tool.resolve_period` | `periods.FiscalCalendar` (any fiscal start, label convention, close lag) |
| `grounding/callbacks.py` | `grounding/guard.py` + `callbacks.py` + evidence ledger/attribution |
| bi_agent + 4 source agents | removed from the number path; `data_explorer` for exploration only |
| ms_sql / redshift / salesforce / oracle MCP servers | one `mcp_server/` image per source (Salesforce: query its Redshift/warehouse replica, as Kaplan's UC5 did) |
| `test_integration_uc_baseline.py` (hard-coded expected values) | `sl_reconciliation_tests` + `fpa reconcile` (client SQL, tolerances, reports) |
| ADK eval sets (`tests/evalsets/*`) | `kind: question` tests + `fpa eval` |
| `gcs_turn_logger.py` | `callbacks._write_turn_log` (now includes number → evidence attribution) |

## Use cases as configuration

```yaml
# UC1 — New starts variance (pvm ∘ mix_rate by channel, timing post-netting)
- analysis_id: uc1_new_starts
  headline_metric: finance_starts
  method: price_volume_mix
  inputs: {value: finance_starts, volume: reportable_leads}
  dimension: channel
  default_comparison: prior_year
  params:
    rate_is_ratio: true
    labels: {volume: Volume Driver, mix: Mix Driver, rate: Rate Driver, unattributed: Re-enrollments (no lead)}
    adjustments: [{label: Timing, metric_id: timing_pulled_starts, sign: -1}]

# UC2 — Census bridge (carry-in + signed flows vs real ending census)
- analysis_id: uc2_census
  headline_metric: census_month_end          # time_semantics: snapshot
  method: rollforward_bridge
  inputs: {level: census_month_end, flows: census_flow_events}   # flows measure: SUM(census_sign)
  params: {flow_dimension: status_code}

# UC3 — Revenue per student (fiscal year, revenue vs enrollment effect)
- analysis_id: uc3_rps
  headline_metric: revenue_per_student       # derived: {recognized_revenue} / {census_students}
  method: ratio_bridge
  inputs: {numerator: recognized_revenue, denominator: census_students}
# "student count by military segment" = get_metric(census_students, group_by=[military_segment]);
# additive: false makes the tool return the true total and flag the category overlap.

# UC4 — Health segment budget vs actuals (units / ASP by modality)
- analysis_id: uc4_health_bva
  headline_metric: net_sales
  method: price_volume_mix
  inputs: {value: net_sales, volume: paid_units, budget_value: budget_net_sales, budget_volume: budget_paid_units}
  dimension: modality
  default_comparison: budget
  params: {rate_is_ratio: false, excluded_categories: [OTHER]}
# run with filters=["vertical=HEALTH"]

# UC5 — Partner sales summary (several independent metrics, no bridge)
# no analysis row: the agent calls get_metric / compare_metric per key metric.
```

Kaplan specifics that become generic settings: Jul–Jun fiscal year → `calendar.fiscal_year_start_month: 7`;
"YTD through last closed month" → `periods` default; January carry-in wrap → `month_before()` in
the roll-forward method; zero-lead re-enrollments → `unattributed` driver; OTHER modality →
`excluded_categories`; distinct-student overcount → `additive: false`.
