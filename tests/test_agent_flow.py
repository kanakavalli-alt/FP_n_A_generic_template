"""
The real ADK runner + the real tools + the real callbacks, driven by a
scripted model (no credentials needed). Proves the wiring end to end:
tool call -> evidence in session state -> grounded answer passes with every
number attributed; a fabricated number is withheld (or regenerated).
"""
from __future__ import annotations

import pytest

adk = pytest.importorskip("google.adk")

from google.adk.agents import Agent  # noqa: E402
from google.adk.models import LlmResponse  # noqa: E402
from google.adk.models.base_llm import BaseLlm  # noqa: E402
from google.adk.runners import InMemoryRunner  # noqa: E402
from google.genai import types  # noqa: E402

from fpa_agent.agents import tools  # noqa: E402
from fpa_agent.agents.root import root_agent  # noqa: E402
from fpa_agent.grounding import callbacks as gcb  # noqa: E402


class ScriptedLlm(BaseLlm):
    model: str = "scripted"
    script: list = []
    calls: int = 0

    async def generate_content_async(self, llm_request, stream: bool = False):
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        yield step(llm_request)


def call_get_metric(_req):
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(
        name="get_metric", args={"metric_id": "active_students", "period_type": "month", "year": 2026, "month": 9}))]))


def last_tool_response(req) -> dict:
    for content in reversed(req.contents):
        for p in content.parts or []:
            if getattr(p, "function_response", None):
                return dict(p.function_response.response)
    raise AssertionError("no tool response in request")


def answer(fn):
    return lambda req: LlmResponse(content=types.Content(role="model", parts=[types.Part(text=fn(req))]))


def make_runner(final_step):
    agent = Agent(name="fpa_analyst", model=ScriptedLlm(script=[call_get_metric, final_step]),
                  instruction="test", tools=root_agent.tools,
                  before_model_callback=gcb.capture_user_numbers,
                  after_tool_callback=gcb.capture_tool_output,
                  after_model_callback=gcb.enforce_grounding)
    return InMemoryRunner(agent=agent, app_name="t")


async def converse(runner, text="What was census at the end of September 2026?"):
    s = await runner.session_service.create_session(app_name="t", user_id="u")
    final = ""
    async for ev in runner.run_async(user_id="u", session_id=s.id,
                                     new_message=types.Content(role="user", parts=[types.Part(text=text)])):
        if ev.is_final_response() and ev.content and ev.content.parts:
            final = "".join(p.text or "" for p in ev.content.parts if getattr(p, "text", None))
    state = (await runner.session_service.get_session(app_name="t", user_id="u", session_id=s.id)).state
    return final, dict(state)


@pytest.fixture(autouse=True)
def no_regen(tmp_path, monkeypatch):
    monkeypatch.setenv("TURN_LOG_DIR", str(tmp_path))
    from fpa_agent import config
    config.settings.cache_clear()
    gcb.configure(regenerator=None)
    yield
    config.settings.cache_clear()


def test_grounded_answer_passes_with_attribution(run):
    runner = make_runner(answer(lambda req: f"Census at the end of September 2026 was "
                                            f"{int(last_tool_response(req)['value']):,} active students."))
    final, state = run(converse(runner))
    assert final.startswith("Census at the end of September 2026 was 8,504")
    audit = state[f"{gcb.AUDIT_PREFIX}1"]
    assert audit["outcome"] == "passed"
    ev_ids = audit["number_attribution"]["8504"]["evidence_ids"]
    assert ev_ids and f"evidence:{ev_ids[0]}" in state
    assert "student_census" in state[f"evidence:{ev_ids[0]}"]["executed_sql"]


def test_fabricated_number_is_withheld(run):
    runner = make_runner(answer(lambda req: "Census was 9,999 students, up 12% year over year."))
    final, state = run(converse(runner))
    assert final == gcb.SAFE_MESSAGE
    audit = state[f"{gcb.AUDIT_PREFIX}1"]
    assert audit["outcome"] == "blocked" and "9999" in audit["violations"]


def test_regeneration_rescues_a_fixable_answer(run):
    gcb.configure(regenerator=lambda text, msg, allowed: "Census at the end of September 2026 was 8,504.")
    runner = make_runner(answer(lambda req: "Census was about 8,600."))
    final, state = run(converse(runner))
    assert final == "Census at the end of September 2026 was 8,504."
    assert state[f"{gcb.AUDIT_PREFIX}1"]["outcome"] == "regenerated"


def test_explain_lineage_from_session_state(run):
    runner = make_runner(answer(lambda req: f"Census was {last_tool_response(req)['value']}."))
    _, state = run(converse(runner))
    ev_id = next(k.split(":", 1)[1] for k in state if k.startswith("evidence:"))

    class Ctx:
        def __init__(self, st):
            self.state = st

    out = run(tools.explain_lineage(ev_id, tool_context=Ctx(state)))
    step = out["lineage"][0]
    assert step["metric"]["term"] == "Census" and step["source_system"] == "Student Information System"
    assert "snapshot_month" in step["executed_sql"]


def test_calculate_refuses_ungrounded_inputs(run):
    class Ctx:
        state = {gcb.TURN_KEY: 1, f"{gcb.CAP_PREFIX}1:x": {"numbers": ["8504", "7650"], "labels": [],
                                                           "evidence_ids": [], "tool": "get_metric",
                                                           "exploratory": False, "returned_data": True}}
    ok = run(tools.calculate("({a} - {b}) / {b} * 100", ["a=8504", "b=7650"], tool_context=Ctx()))
    assert ok["status"] == "ok" and ok["result_2dp"] == "11.16"
    bad = run(tools.calculate("{a} * 2", ["a=1234"], tool_context=Ctx()))
    assert bad["status"] == "declined"


def test_tool_errors_are_structured(run):
    out = run(tools.get_metric("marketing_spend", "month", year=2026, month=9))
    assert out["status"] == "declined" and "Marketing Finance" in out["reason"]
    out = run(tools.get_metric("leads", "month", year=2026, month=9, group_by=["modality"]))
    assert out["status"] == "error" and "modality" in out["reason"]
    out = run(tools.get_metric("leads", "month", year=2026, month=9, filters=["program=Nursing|IT"]))
    assert out["status"] == "ok" and int(out["value"]) > 0
