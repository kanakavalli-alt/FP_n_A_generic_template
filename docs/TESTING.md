# Testing and source reconciliation

Four layers, from fast and offline to live and end to end.

| Layer | Command | Needs | Proves |
|---|---|---|---|
| Unit + integration | `python -m pytest` | nothing (builds demo DBs in a temp dir) | compiler safety, period math, netting on random inputs, service vs raw SQL, guard rules, validator, MCP server tools, **real ADK runner + callbacks with a scripted model** |
| Catalogue lint | `python -m fpa_agent validate [--live]` | catalogue (+ sources for `--live`) | every row is consistent; every referenced column exists |
| **Source reconciliation** | `python -m fpa_agent reconcile [--auto-months N] [--tags ...]` | sources | the agent's number == the client's own query / report figure |
| End-to-end questions | `python -m fpa_agent eval` | sources + model | routing picks the right tool, grounding passes, the reference figure appears in the answer, every number is attributed |

## Source reconciliation in detail
For each test the runner executes:

* **agent side** — exactly the code path the tools use (`MetricService.fetch` / `run_analysis`):
  catalogue → compiled SQL → source → Decimal math;
* **reference side** — independent of the catalogue: the client's own SQL (read-only, named
  parameters filled from the resolved period) or a literal from their report;
* **verdict** — `|agent − reference| ≤ max(tolerance_abs, tolerance_pct·|reference|)`, per group for
  breakdowns; analyses must also net exactly and their *rounded* drivers must sum to the rounded
  change.

`--auto-months N` adds a test for every confirmed metric with catalogue `reference_sql`, for each
of the last N closed months and the last closed fiscal year — so new metrics are reconciled
without writing tests by hand.

Outputs (`--out`): `reconciliation.json` (machine), `reconciliation.md` (client sign-off, with
both queries side by side) and `reconciliation.junit.xml` (CI). Exit code ≠ 0 on any failure.

The suite proves it can fail: `tests/test_reconciliation.py` feeds a reference that forgets a
filter (the class of bug behind Kaplan's "starts didn't tie" UAT ticket) and a grouped reference
off by one in one category; both are caught and reported at the right granularity.

## Grounding audit trail
Every answered turn writes a record (local `logs/turns/*.jsonl` or `TURN_LOG_GCS_BUCKET`):
`outcome` (passed | regenerated | blocked), `final_answer`, `draft_answer` (if changed),
`violations`, `number_attribution` (each number → match kind → evidence ids → tools), and the
evidence bodies (definition hash, source, period, executed SQL, values). In a conversation the
user can ask "where does that number come from?" and the agent calls `explain_lineage`.

## CI
`.github/workflows/ci.yml` runs build-demo → validate → pytest → reconcile and uploads the
reconciliation report. For a client, point the reconcile step at a staging catalogue/sources.
