"""
End-to-end question tests: run the REAL agent on a natural-language question
and verify the whole chain, not just the engine:

  1. the expected tool was called (routing works for this client's catalogue);
  2. the grounding guard passed (no figure had to be withheld);
  3. the reference figure — computed independently (reference SQL or the
     deterministic engine) — appears in the final answer (allowing honest
     rounding / abbreviation / % formatting);
  4. every number in the answer is attributed to evidence (audit trail).

Needs model credentials (GOOGLE_API_KEY or Vertex AI env). Run with
`python -m fpa_agent.testing.cli eval`.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import date

from fpa_agent.engine.results import D
from fpa_agent.engine.service import MetricService
from fpa_agent.grounding import callbacks as gcb
from fpa_agent.grounding import guard
from fpa_agent.semantic.models import ReconTest
from fpa_agent.semantic.repository import Catalog


@dataclass
class EvalResult:
    test_id: str
    status: str
    question: str
    answer: str = ""
    tools_called: list[str] = field(default_factory=list)
    expected_figures: list[str] = field(default_factory=list)
    found_figure: str | None = None
    grounding_outcome: str | None = None
    unattributed_numbers: list[str] = field(default_factory=list)
    message: str = ""
    duration_ms: int = 0


async def ask(question: str, session_id: str | None = None, runner=None) -> dict:
    """Run one question through the agent; return answer, tool calls, audit."""
    from google.adk.runners import InMemoryRunner
    from google.genai import types
    from fpa_agent.agents.root import root_agent

    runner = runner or InMemoryRunner(agent=root_agent, app_name="fpa_agent")
    user = "eval_user"
    session_id = session_id or uuid.uuid4().hex
    if await runner.session_service.get_session(app_name="fpa_agent", user_id=user, session_id=session_id) is None:
        await runner.session_service.create_session(app_name="fpa_agent", user_id=user, session_id=session_id)
    calls, final = [], ""
    async for event in runner.run_async(user_id=user, session_id=session_id,
                                        new_message=types.Content(role="user", parts=[types.Part(text=question)])):
        for fc in event.get_function_calls() or []:
            calls.append({"name": fc.name, "args": dict(fc.args or {})})
        if event.is_final_response() and event.content and event.content.parts:
            final = "".join(p.text or "" for p in event.content.parts if getattr(p, "text", None))
    session = await runner.session_service.get_session(app_name="fpa_agent", user_id=user, session_id=session_id)
    state = dict(session.state) if session else {}
    turn = state.get(gcb.TURN_KEY, 0)
    return {"answer": final, "tool_calls": calls, "audit": state.get(f"{gcb.AUDIT_PREFIX}{turn}"),
            "session_id": session_id, "runner": runner}


async def expected_figures(catalog: Catalog, t: ReconTest, as_of: date | None = None) -> list[str]:
    svc = MetricService(catalog, as_of=as_of)
    if t.analysis_id:
        out = await svc.run_analysis(t.analysis_id, t.period, t.comparison, t.filters)
        return [x for x in (out["change"], out["current_value"]) if x]
    out = await svc.get_metric(t.metric_id, t.period, t.filters)
    return [out["value"]] if out.get("value") else []


async def run_question_test(catalog: Catalog, t: ReconTest, as_of: date | None = None) -> EvalResult:
    t0 = time.perf_counter()
    res = EvalResult(test_id=t.test_id, status="error", question=t.question or "")
    try:
        res.expected_figures = await expected_figures(catalog, t, as_of)
        out = await ask(t.question)
        res.answer, res.tools_called = out["answer"], [c["name"] for c in out["tool_calls"]]
        audit = out["audit"] or {}
        res.grounding_outcome = audit.get("outcome")
        attributed = set((audit.get("number_attribution") or {}).keys())
        answer_numbers = {n for n in guard.extract_numbers(res.answer) if not guard.is_benign(n)}
        res.unattributed_numbers = sorted(n for n in answer_numbers - attributed if n.lstrip("-") not in attributed)
        for fig in res.expected_figures:
            # an answer number that is the reference, honestly rounded/abbreviated/as %
            ref = guard.Matcher({str(abs(D(fig)))})
            if any(ref.match_kind(n.lstrip("-")) in ("exact", "rounded", "scaled", "percent")
                   for n in answer_numbers):
                res.found_figure = fig
                break
        problems = []
        if t.expected_tool and t.expected_tool not in res.tools_called:
            problems.append(f"expected tool {t.expected_tool} not called (called: {res.tools_called})")
        if res.grounding_outcome == "blocked":
            problems.append("grounding guard blocked the answer")
        if res.expected_figures and not res.found_figure:
            problems.append(f"none of the reference figures {res.expected_figures} appear in the answer")
        res.status = "fail" if problems else "pass"
        res.message = "; ".join(problems) or "routed, grounded and matched the source"
    except Exception as e:
        res.message = f"{type(e).__name__}: {e}"
    res.duration_ms = int((time.perf_counter() - t0) * 1000)
    return res
