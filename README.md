# FP&A Agent Template

A generic, client-agnostic FP&A analyst agent: plain-English finance questions
answered live from any number of source systems, with every figure

* **defined** by a governed semantic layer (BigQuery — the single source of truth),
* **computed by code** (compiled SQL + Decimal math), never by the model,
* **checked** by a grounding guard before the user sees it, and
* **traceable** to the exact SQL and source that produced it.

It generalises the Kaplan FP&A build (4 use cases, 13/13 figures matched the
source systems) so that the next client is **catalog rows, not code**.

```
                 ┌──────────────────────────── semantic layer (BigQuery) ───────────────────────────┐
                 │ client_profile · sources · dimensions · metrics · analyses · glossary ·            │
                 │ agent_profiles · reconciliation_tests                                              │
                 └───────────────┬──────────────────────────────┬─────────────────────────────────────┘
       persona + knowledge base  │                              │ definitions, formulas, methods
                                 ▼                              ▼
 user ─▶ fpa_analyst (ADK) ──▶ generic tools ──▶ MetricService ──▶ QuerySpec ──▶ MCP server ──▶ source DB
              ▲   │            get_metric        gate · period ·     (compiled,     (OIDC, read-only,
              │   │            compare_metric    fetch · netting     bound params)   allow-listed)
              │   │            run_analysis      · evidence ledger
              │   └─▶ data_explorer (exploratory only)
              └──── grounding guard: every number in the answer must trace to evidence this turn
```

## What changed versus the Kaplan build

| Kaplan (client-specific) | Template (generic) |
|---|---|
| `uc1/uc2/uc3/uc4` dedicated tools (3,200 lines) to avoid nested-agent latency | 3 generic one-hop tools — `get_metric`, `compare_metric`, `run_analysis` — driven by catalog rows; same latency benefit, any client |
| root → bi_agent → 4 source agents (65–230 s/question) | flat: root + one exploratory sub-agent; governed numbers are one tool call away |
| Headline SQL hand-written in `*_queries.py` | SQL **compiled** from the metric row (measure, filters, time binding, dimensions, joins) |
| Variance chains wired in `resolver.py` / `fetch_plan.py` per use case | **analysis rows** bind a generic method to metrics by role; period arithmetic in `periods.py` |
| Derived math in Python | **formula** column evaluated in Decimal (`{revenue} / {students}`) |
| Prompt hand-written with Kaplan rules (2,500 lines) | prompt **assembled each turn** from persona + client profile + catalog |
| One MCP server per database | **one generic MCP server** image, configured by env |
| Manual reconciliation before UAT | `fpa reconcile` — agent vs the client's own SQL, JSON/Markdown/JUnit reports, CI gate |

## Quick start (demo client, no cloud needed)

```bash
python -m pip install -r requirements-dev.txt
```
```bash
python demo/build_demo_db.py
```
```bash
python -m fpa_agent validate --live
```
```bash
python -m fpa_agent reconcile --auto-months 3
```
```bash
python -m pytest
```

Talk to the agent (needs Gemini credentials in `.env`, see `.env.example`):

```bash
adk web apps
```
```bash
python -m fpa_agent ask "Why did new starts change in fiscal Q3 2026 versus last year?"
```

## Repository map

| Path | What it is |
|---|---|
| `fpa_agent/semantic/` | models, BigQuery/YAML repository (TTL-cached), search, validator, loader, BQ schema |
| `fpa_agent/periods.py` | fiscal-calendar period resolution (no date math in the model) |
| `fpa_agent/query/compiler.py` | metric → safe parameterised SQL for 8 dialects |
| `fpa_agent/connectors/` | local DB-API connector and authenticated MCP client |
| `fpa_agent/engine/` | primitives (netting-guaranteed), formulas, methods, `MetricService` |
| `fpa_agent/grounding/` | evidence ledger, grounding guard, ADK callbacks, audit log |
| `fpa_agent/agents/` | generic tools, prompt assembly, agent topology |
| `fpa_agent/testing/` | source reconciliation runner, end-to-end question eval |
| `mcp_server/` | generic read-only MCP server (+ OIDC auth, Dockerfile) |
| `semantic_layer/demo/` | complete demo client catalogue |
| `semantic_layer/client_template/` | commented starting point for a new client |
| `semantic_layer/bigquery/ddl.sql` | BigQuery tables |
| `apps/fpa_analyst/` | ADK entry point (`adk web apps`) |
| `deploy/` | Agent Engine deployment |
| `docs/` | architecture, onboarding playbook, semantic-layer reference, testing, Kaplan migration |

Start with [docs/ONBOARDING.md](docs/ONBOARDING.md) for a new client.
