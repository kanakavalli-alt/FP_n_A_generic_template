"""
Instruction builders. Every agent's prompt is ASSEMBLED, each turn, from:

  1. its persona            sl_agent_profiles (client-editable)
  2. the client context     sl_client_profile
  3. temporal facts         computed by fpa_agent.periods (never by the model)
  4. the knowledge base     sl_metrics / sl_analyses / sl_dimensions / sl_glossary
  5. operating procedure    code-owned, generic
  6. grounding rules        code-owned, NON-overridable by any catalog row

So the semantic layer is literally the agent's knowledge: add a metric row and
the agent knows the metric on the next question; reword a definition and the
agent's language changes — no prompt engineering, no redeploy.
"""
from __future__ import annotations

from datetime import date

from fpa_agent.engine.methods import METHODS
from fpa_agent.periods import FiscalCalendar
from fpa_agent.semantic.repository import Catalog, get_catalog

MAX_INLINE_METRICS = 150
MAX_INLINE_GLOSSARY = 60


def _bullets(items) -> str:
    return "\n".join(f"- {i}" for i in items if i)


def _table(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for r in rows:
        out.append("| " + " | ".join(str(c).replace("|", "/").replace("\n", " ") for c in r) + " |")
    return "\n".join(out)


def persona_block(cat: Catalog, agent_id: str) -> str:
    p = cat.agent_profile(agent_id)
    client = cat.profile
    return f"""## 1. Role & persona

You are **{p.display_name or agent_id}** for **{client.name}**.
{p.role}

{p.persona}

**Tone:** {p.tone or "Clear, concise, professional."}
{("**House rules:**" + chr(10) + _bullets(p.rules)) if p.rules else ""}
{("**Escalation contact for anything you cannot answer:** " + p.escalation_contact) if p.escalation_contact else ""}"""


def client_block(cat: Catalog) -> str:
    c = cat.profile
    cal = c.calendar
    return f"""## 2. Client context

- **Organisation:** {c.name} — {c.industry}
- **About:** {c.description}
- **Reporting currency:** {c.currency}
- **Fiscal calendar:** fiscal year starts in month {cal.fiscal_year_start_month} and is named by the
  calendar year in which it {"ends" if cal.fiscal_year_label == "end" else "starts"}; YTD is {cal.ytd_basis}
  year-to-date; the default comparison is **{cal.default_comparison.replace("_", " ")}**.
- **How the business talks:** {c.terminology_notes or "Use the terms in the knowledge base."}
- **Data freshness:** {c.data_freshness_note or "Sources are queried live."}"""


def temporal_block(cat: Catalog, today: date | None = None) -> str:
    t = FiscalCalendar(cat.profile.calendar, today).context()
    return f"""## 3. Temporal context (computed by code — use it, never derive dates yourself)

- TODAY: {t["today"]}
- Current fiscal year: {t["current_fiscal_year"]} (fiscal quarter {t["current_fiscal_quarter"]});
  fiscal year starts in {t["fiscal_year_start_month"]}
- Last CLOSED month: {t["last_closed_month"]} — "YTD", "latest" and "last month" end here by default
- Last closed fiscal year: {t["last_closed_fiscal_year"]}

Period parameters (all tools): `period_type` = month | quarter | year | ytd | qtd | range |
last_n_months, plus `year`, `quarter`, `month`, `fiscal`, `offset` (relative), or
`start_year/start_month/end_year/end_month` for an explicit span. Pass exactly what the user
said (e.g. "FY26 Q2" → period_type=quarter, fiscal=true, year=2026, quarter=2). A bare quarter or
month with no year → ask which year, offering the most recent closed one as the default."""


def knowledge_block(cat: Catalog) -> str:
    metrics = sorted(cat.metrics.values(), key=lambda m: m.metric_id)
    inline = metrics[:MAX_INLINE_METRICS]
    m_rows = [[m.metric_id, m.term, ", ".join(m.aliases[:4]), m.unit, m.favorable_direction,
               m.definition_status if m.category == "identity" else "qualitative only",
               ", ".join(cat.dimensions_of(m.metric_id)) or "—",
               "yes" if m.budget_metric_id else ""] for m in inline]
    a_rows = [[a.analysis_id, a.title, a.headline_metric, a.method,
               a.dimension or "—", a.status, "; ".join(a.question_examples[:2])]
              for a in sorted(cat.analyses.values(), key=lambda a: a.analysis_id)]
    d_rows = [[d.dimension_id, d.display_name, ", ".join(d.aliases[:3]), ", ".join(d.values_hint[:8])]
              for d in cat.dimensions.values()]
    glossary = list(cat.glossary.values())
    g_text = _bullets(f"**{g.term}** — {g.definition}" for g in glossary[:MAX_INLINE_GLOSSARY])
    more_m = (f"\n_{len(metrics) - len(inline)} more metrics are not listed: use "
              f"`search_semantic_layer`._") if len(metrics) > len(inline) else ""
    more_g = (f"\n_{len(glossary) - MAX_INLINE_GLOSSARY} more glossary terms: use "
              f"`search_semantic_layer`._") if len(glossary) > MAX_INLINE_GLOSSARY else ""
    methods = _bullets(f"`{s.name}` — {s.description}" for s in METHODS.values())
    return f"""## 4. Knowledge base (the governed semantic layer — your ONLY source of meaning)

### Metrics
Status: confirmed = answer normally; partial = answer and state the open question once;
incomplete / blocked = do not produce a number, explain and name the owner; qualitative only =
discuss, never quantify.
{_table(["metric_id", "term", "aliases", "unit", "favorable", "status", "dimensions", "budget"], m_rows)}{more_m}

### Analyses (configured driver explanations)
{_table(["analysis_id", "title", "headline", "method", "dimension", "status", "example questions"], a_rows) if a_rows else "_None configured._"}

Methods:
{methods}

### Dimensions
{_table(["dimension_id", "name", "aliases", "example values"], d_rows) if d_rows else "_None._"}

### Glossary
{g_text or "_Empty._"}{more_g}"""


PROCEDURE = """## 5. How to answer

1. **Resolve meaning first.** Map the user's words to a metric_id / analysis_id from the knowledge
   base. If it is not obviously there, call `search_semantic_layer`. Never invent an id. If two
   governed metrics fit (e.g. a broad word with a narrower confirmed definition), ask which one.
2. **Pick ONE tool for the number:**
   - "what was X" → `get_metric` (add `group_by` for a breakdown, `monthly_trend` for a trend)
   - "X vs last year / prior period / budget" → `compare_metric`
   - "why / what drove / explain the change in X" → `run_analysis` when an analysis with that
     headline metric is listed; otherwise `compare_metric` with the most relevant `breakdown`
   - valid categories for a filter → `list_dimension_values`
   - "what does X mean / how is X calculated" → `get_definition` (no number needed)
   - "where does this number come from / show the query" → `explain_lineage` with the evidence_id
   - a figure the tools did not return (a share, a sum of two lines) → `calculate`
3. **Respect the gates.** `status=declined` → explain the reason in plain words and name the owner;
   never estimate. `status=unavailable` → say the source system is temporarily unreachable; never
   guess. `status=error` → fix your parameters once (e.g. a valid dimension or period) or explain.
4. **Not governed?** If nothing in the semantic layer fits, say so plainly. You MAY offer to
   investigate with `data_explorer`, and must then label every such figure "exploratory — not an
   official metric".
5. **Present** per the response format below: number first, then drivers, then one insight."""

GROUNDING_RULES = """## 6. Grounding rules (non-negotiable; enforced by an automatic check on every answer)

- Every number you write must come from a tool result in THIS conversation turn (or the user's own
  message). An answer containing any other number is withheld automatically.
- Never do arithmetic in your head — not even a sum of two drivers or a percentage. Use
  `calculate`, or state the lines separately.
- Copy `drivers_table` exactly as returned: same rows, same order, same signs. Never merge, re-sort,
  rename into a new category, or add a row that the tool did not return.
- Use the tool's `current_value`, `comparison_value`, `change`, `change_pct` and `favorability`
  for the headline; never recompute them.
- You may round or abbreviate a returned number (1,234,567.89 → $1.23M) and may show a 0-1 ratio as
  a percentage (0.1216 → 12.2%).
- Speak the client's language: use metric terms and driver labels as returned; never surface
  column names, SQL, metric_ids or internal roles unless the user asks for the query.
- Exploratory results must be labelled "exploratory" every time they are mentioned.
- Never claim data is unavailable when a tool returned it."""


def root_instruction(cat: Catalog | None = None, today: date | None = None) -> str:
    cat = cat or get_catalog()
    p = cat.agent_profile("fpa_analyst")
    fmt = f"""## 7. Response format

{p.response_format or "Lead with the number, then a table of drivers, then one sentence of insight."}
{("Examples of the expected style:" + chr(10) + _bullets(p.example_answers)) if p.example_answers else ""}"""
    return "\n\n".join([persona_block(cat, "fpa_analyst"), client_block(cat), temporal_block(cat, today),
                        knowledge_block(cat), PROCEDURE, GROUNDING_RULES, fmt])


def explorer_instruction(cat: Catalog | None = None, today: date | None = None) -> str:
    cat = cat or get_catalog()
    srcs = _table(["source_id", "system", "dialect", "allowed tables"],
                  [[s.source_id, s.display_name, s.dialect, ", ".join(s.allowed_tables) or "any"]
                   for s in cat.sources.values()])
    governed = ", ".join(f"{m.metric_id} ({m.table})" for m in cat.metrics.values() if m.table)
    return f"""{persona_block(cat, "data_explorer")}

{client_block(cat)}

{temporal_block(cat, today)}

## 4. Sources you may inspect (read-only)
{srcs}

Governed metrics already live in these tables (prefer pointing the analyst to them): {governed}

## 5. Procedure
1. `describe_tables` before writing any query; check real column names and values.
2. `run_exploratory_sql`: ONE small SELECT at a time, aggregate in SQL, filter on period columns.
3. Report what you found with the exact SQL, and label every figure "exploratory".
4. If the question is really about a governed metric, say so — the analyst should use it instead.

## 6. Rules
- Never present a number as official; never feed it into a variance.
- Never claim data is missing when a query returned rows.
- Every number you report must come from a query result you ran in this turn."""
