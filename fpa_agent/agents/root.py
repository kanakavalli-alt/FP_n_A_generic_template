"""
Agent topology (deliberately flat — the Kaplan latency lesson):

    fpa_analyst (root, conversational)
      ├─ search_semantic_layer / get_definition           (knowledge)
      ├─ get_metric / compare_metric / run_analysis        (one-hop deterministic numbers)
      ├─ list_dimension_values / calculate / explain_lineage
      └─ data_explorer (AgentTool, exploratory only)
            ├─ describe_tables
            └─ run_exploratory_sql

Kaplan's root -> bi_agent -> 4 source agents chain cost 65-230 s per question
and produced most of its failures at LLM hand-off points. Here every governed
number is one tool call from the root; the only nested agent is the
exploratory explorer, which never produces an official figure.
"""
from __future__ import annotations

from google.adk.agents import Agent
from google.adk.tools import FunctionTool
from google.adk.tools.agent_tool import AgentTool
from google.genai import types

from fpa_agent.agents import prompts, tools
from fpa_agent.config import settings
from fpa_agent.grounding import callbacks as gcb
from fpa_agent.semantic.repository import get_catalog


def _header_keywords() -> set[str]:
    cat = get_catalog()
    words = {"driver", "category"}
    for d in cat.dimensions.values():
        words.add(d.dimension_id.replace("_", " ").lower())
        if d.display_name:
            words.add(d.display_name.lower())
        words.update(a.lower() for a in d.aliases)
    return words


gcb.configure(
    regenerator=gcb.gemini_regenerator(settings().regeneration_model_name)
    if settings().grounding_regenerate else None,
    header_keywords_provider=_header_keywords,
)


def _root_instruction(_ctx) -> str:
    return prompts.root_instruction(get_catalog())


def _explorer_instruction(_ctx) -> str:
    return prompts.explorer_instruction(get_catalog())


data_explorer = Agent(
    name="data_explorer",
    model=settings().explorer_model_name,
    description="Read-only data investigator for questions the semantic layer does not cover. "
                "Every result is exploratory, never an official figure.",
    instruction=_explorer_instruction,
    generate_content_config=types.GenerateContentConfig(temperature=0.1),
    tools=[FunctionTool(tools.describe_tables), FunctionTool(tools.run_exploratory_sql)],
    before_model_callback=gcb.capture_user_numbers,
    after_tool_callback=gcb.capture_tool_output,
    after_model_callback=gcb.enforce_grounding,
)

root_agent = Agent(
    name="fpa_analyst",
    model=settings().model_name,
    description="FP&A analyst agent: answers finance questions from live source systems using the "
                "governed semantic layer; every figure is computed by code and traceable to its query.",
    instruction=_root_instruction,
    generate_content_config=types.GenerateContentConfig(temperature=0.1),
    tools=[
        FunctionTool(tools.search_semantic_layer),
        FunctionTool(tools.get_definition),
        FunctionTool(tools.get_metric),
        FunctionTool(tools.compare_metric),
        FunctionTool(tools.run_analysis),
        FunctionTool(tools.list_dimension_values),
        FunctionTool(tools.calculate),
        FunctionTool(tools.explain_lineage),
        AgentTool(agent=data_explorer),
    ],
    before_model_callback=gcb.capture_user_numbers,
    after_tool_callback=gcb.capture_tool_output,
    after_model_callback=gcb.enforce_grounding,
)
