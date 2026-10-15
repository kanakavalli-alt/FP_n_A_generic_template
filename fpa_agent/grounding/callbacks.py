"""
ADK wiring for the grounding guard and the per-turn audit trail.

  before_model_callback  capture_user_numbers   new-turn detection + user-stated numbers
  after_tool_callback    capture_tool_output    numbers, labels and evidence ids per call
  after_model_callback   enforce_grounding      verdict -> pass / regenerate / safe message,
                                                plus an audit record attributing every
                                                number in the answer to evidence ids

State layout (all JSON-serialisable, per session):
  _g_turn                       current genuine user-turn number
  _g_cap:<turn>:<uuid>          one tool call's Capture (per-call keys: no read-modify-write
                                race when ADK runs a turn's tool calls concurrently)
  _g_user:<turn>                numbers the user typed this turn
  evidence:<id>                 Evidence records written by the tools
  _g_audit:<turn>               the turn's audit record
Captures from the current and previous turn are honoured (a 2-turn window,
as in the Kaplan build), so "and what about the prior year?" follow-ups work
without letting stale numbers authorise new ones indefinitely.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from fpa_agent.grounding import guard

log = logging.getLogger(__name__)

TURN_KEY = "_g_turn"
CAP_PREFIX = "_g_cap:"
USER_PREFIX = "_g_user:"
AUDIT_PREFIX = "_g_audit:"
TOOL_CALLED_KEY = "_g_tool_called"
WINDOW = 2
EXPLORATORY_TOOLS = {"data_explorer", "run_exploratory_sql", "describe_tables", "sample_values"}
SAFE_MESSAGE = ("I couldn't confirm every figure in that answer against the source systems, so I "
                "haven't shown it. Please ask again, or narrow the question (one metric and one period).")

# Optional hook: (original_text, guard_message, allowed_numbers) -> rewritten text | None
Regenerator = Callable[[str, str, list[str]], "str | None"]
_regenerator: Regenerator | None = None
_header_keywords_provider: Callable[[], set[str]] = lambda: {"driver", "category"}


def configure(regenerator: Regenerator | None = None,
              header_keywords_provider: Callable[[], set[str]] | None = None) -> None:
    global _regenerator, _header_keywords_provider
    _regenerator = regenerator
    if header_keywords_provider:
        _header_keywords_provider = header_keywords_provider


# ── helpers ──────────────────────────────────────────────────────────────────
def _state_items(state) -> list[tuple[str, Any]]:
    to_dict = getattr(state, "to_dict", None)
    return list(to_dict().items()) if callable(to_dict) else list(dict(state).items())


def _in_window(key: str, prefix: str, turn: int) -> bool:
    try:
        k_turn = int(key[len(prefix):].split(":", 1)[0])
    except ValueError:
        return False
    return max(0, turn - WINDOW + 1) <= k_turn <= turn


def captures_in_window(state) -> list[guard.Capture]:
    turn = state.get(TURN_KEY, 0)
    out = []
    for k, v in _state_items(state):
        if k.startswith(CAP_PREFIX) and isinstance(v, dict) and _in_window(k, CAP_PREFIX, turn):
            out.append(guard.Capture(**v))
    return out


def user_numbers_in_window(state) -> set[str]:
    turn = state.get(TURN_KEY, 0)
    out: set[str] = set()
    for k, v in _state_items(state):
        if k.startswith(USER_PREFIX) and isinstance(v, list) and _in_window(k, USER_PREFIX, turn):
            out.update(v)
    return out


def is_real_user_message(content) -> bool:
    """Gemini packages function responses as role="user" too; a genuine user
    message has no function_response parts."""
    if getattr(content, "role", None) != "user":
        return False
    parts = getattr(content, "parts", None) or []
    return bool(parts) and not any(getattr(p, "function_response", None) for p in parts)


def response_text(llm_response) -> str:
    content = getattr(llm_response, "content", None)
    if content is None or not getattr(content, "parts", None):
        return ""
    return "".join(p.text for p in content.parts if getattr(p, "text", None))


def _has_function_call(llm_response) -> bool:
    content = getattr(llm_response, "content", None)
    return any(getattr(p, "function_call", None) for p in (getattr(content, "parts", None) or []))


def _unwrap(tool_response):
    """MCP-style envelopes ({"content":[{"type":"text","text": "<json>"}]}) and
    AgentTool string results -> a dict/str payload."""
    if isinstance(tool_response, dict):
        content = tool_response.get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict) \
                and content[0].get("type") == "text":
            try:
                return json.loads(content[0].get("text", ""))
            except (json.JSONDecodeError, TypeError):
                return content[0].get("text", "")
        if "result" in tool_response and len(tool_response) == 1:
            return tool_response["result"]
    return tool_response


# ── callbacks ────────────────────────────────────────────────────────────────
def capture_user_numbers(callback_context, llm_request):
    contents = getattr(llm_request, "contents", None) or []
    user_msgs = [c for c in contents if is_real_user_message(c)]
    turn = len(user_msgs)
    state = callback_context.state
    if turn > state.get(TURN_KEY, 0):
        state[TURN_KEY] = turn
        state[TOOL_CALLED_KEY] = False
        if user_msgs:
            text = " ".join(getattr(p, "text", "") or "" for p in user_msgs[-1].parts)
            state[f"{USER_PREFIX}{turn}"] = sorted(guard.extract_numbers(text))
    return None


def capture_tool_output(tool, args, tool_context, tool_response):
    state = tool_context.state
    state[TOOL_CALLED_KEY] = True
    payload = _unwrap(tool_response)
    name = getattr(tool, "name", "") or ""
    numbers = guard.numbers_in_payload(payload)
    # The model's own arguments are not evidence: drop numbers that only
    # appear in what it asked for (e.g. a year) is unnecessary — years are
    # benign — but never ADD argument numbers to the allow-list.
    evidence_ids: list[str] = []
    exploratory = name in EXPLORATORY_TOOLS
    returned_data = False
    if isinstance(payload, dict):
        for k in ("evidence_id",):
            if payload.get(k):
                evidence_ids.append(payload[k])
        evidence_ids += list(payload.get("input_evidence_ids") or [])
        exploratory = exploratory or bool(payload.get("exploratory"))
        returned_data = bool(payload.get("rows")) or payload.get("value") is not None
    elif isinstance(payload, str) and exploratory:
        returned_data = bool(numbers)
    cap = guard.Capture(numbers=sorted(numbers), labels=sorted(guard.labels_in_payload(payload)),
                        evidence_ids=evidence_ids, tool=name, exploratory=exploratory,
                        returned_data=returned_data)
    turn = state.get(TURN_KEY, 0)
    state[f"{CAP_PREFIX}{turn}:{uuid.uuid4().hex[:12]}"] = asdict(cap)
    return None


def enforce_grounding(callback_context, llm_response):
    if getattr(llm_response, "partial", False) or _has_function_call(llm_response):
        return None
    text = response_text(llm_response)
    state = callback_context.state
    if not text or not state.get(TOOL_CALLED_KEY):
        # Nothing fetched this turn -> no tool figure can have been fabricated
        # (clarifying questions, greetings, definitions from the prompt).
        return None
    captures = captures_in_window(state)
    verdict = guard.check(text, captures, user_numbers_in_window(state), _header_keywords_provider())
    final_text, outcome = text, "passed"
    if not verdict.ok:
        outcome = "blocked"
        regenerated = None
        if _regenerator is not None:
            allowed = sorted({n for c in captures for n in c.numbers})
            try:
                regenerated = _regenerator(text, verdict.message(), allowed)
            except Exception:  # never let the safety net crash the turn
                log.exception("grounded regeneration failed")
        if regenerated:
            v2 = guard.check(regenerated, captures, user_numbers_in_window(state), _header_keywords_provider())
            if v2.ok:
                final_text, outcome, verdict = regenerated, "regenerated", v2
        if outcome == "blocked":
            final_text = SAFE_MESSAGE
    _audit(callback_context, text, final_text, outcome, verdict, captures)
    if final_text == text:
        return None
    from google.adk.models import LlmResponse
    from google.genai import types
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=final_text)]))


# ── audit trail ──────────────────────────────────────────────────────────────
def _audit(callback_context, draft: str, final: str, outcome: str, verdict: guard.Verdict,
           captures: list[guard.Capture]) -> None:
    state = callback_context.state
    turn = state.get(TURN_KEY, 0)
    record = {
        "ts": time.time(), "turn": turn, "outcome": outcome,
        "agent": getattr(callback_context, "agent_name", ""),
        "session_id": getattr(getattr(callback_context, "session", None), "id", None),
        "final_answer": final,
        "draft_answer": draft if outcome != "passed" else None,
        "violations": None if verdict.ok else verdict.message(),
        "number_attribution": verdict.attribution,
        "tools_used": sorted({c.tool for c in captures if c.tool}),
        "evidence_ids": sorted({e for c in captures for e in c.evidence_ids}),
    }
    state[f"{AUDIT_PREFIX}{turn}"] = record
    _write_turn_log(record, state)


def _write_turn_log(record: dict, state) -> None:
    from fpa_agent.config import settings
    try:
        evidence = {k.split(":", 1)[1]: v for k, v in _state_items(state)
                    if k.startswith("evidence:") and k.split(":", 1)[1] in record["evidence_ids"]}
        line = json.dumps({**record, "evidence": evidence}, default=str)
        bucket = os.getenv("TURN_LOG_GCS_BUCKET")
        if bucket:
            from google.cloud import storage
            name = f"turns/{time.strftime('%Y/%m/%d')}/{record.get('session_id')}_{record['turn']}_{uuid.uuid4().hex[:6]}.json"
            storage.Client().bucket(bucket).blob(name).upload_from_string(line, "application/json")
            return
        d = Path(settings().turn_log_dir)
        d.mkdir(parents=True, exist_ok=True)
        with open(d / f"{time.strftime('%Y-%m-%d')}.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        log.exception("turn log write failed (non-fatal)")


def gemini_regenerator(model: str) -> Regenerator:
    """Default regenerator: one constrained rewrite using only grounded numbers."""
    def _regen(original: str, guard_message: str, allowed: list[str]) -> str | None:
        from google import genai
        from google.genai import types
        prompt = (
            "Rewrite the financial answer below so that EVERY number in it appears in the "
            "ALLOWED NUMBERS list (you may round or abbreviate them, e.g. 1,234,567 -> 1.23M). "
            "Remove any sentence whose figure cannot be supported. Do not add new numbers, "
            "do not mention this instruction.\n\n"
            f"PROBLEM: {guard_message}\n\nALLOWED NUMBERS: {', '.join(allowed[:400])}\n\n"
            f"ANSWER:\n{original}"
        )
        resp = genai.Client().models.generate_content(
            model=model, contents=prompt, config=types.GenerateContentConfig(temperature=0))
        return (resp.text or "").strip() or None
    return _regen
