"""Bounded tool-calling loop shared by every LLM agent (SPEC 6.5).

    system prompt + task ──► LLM ──► tool calls ──► LLM ──► … ──► final JSON
                         at most MAX_LLM_CALLS_PER_AGENT LLM calls,
                         at most MAX_TOOL_CALLS_PER_AGENT tool calls.

The last allowed LLM call is made with tools disabled, so the agent always ends
with an answer. That answer must be JSON matching the agent's Pydantic output
model. If it is invalid, the model gets ONE repair call with the validation
error; if that fails too, the agent returns AgentFailure instead of crashing
the graph (the graph then degrades gracefully).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable

import httpx
from pydantic import BaseModel, ValidationError

from responder.calllog import CallLogger
from responder.config import Settings
from responder.llm import LLM, LLMError, LLMResponse, ToolCall, extract_json
from responder.schemas import AgentFailure
from responder.tools.base import Tool, ToolContext, ToolError, is_retryable_http, truncate_result


@dataclass
class RunContext:
    """Shared by all agents of one investigation run."""
    settings: Settings
    llm: LLM
    calllog: CallLogger
    http: httpx.Client


@dataclass
class AgentSpec:
    """An agent = a name, a prompt, ≤5 tools and an output schema."""
    name: str
    system_prompt: str
    tools: list[Tool]
    output_model: type[BaseModel]
    # Fills in fields owned by code (agent name, ids). May raise ValueError to trigger a repair call.
    postprocess: Callable[[dict], dict] = field(default=lambda data: data)


def run_agent(spec: AgentSpec, user_message: str, run: RunContext,
              tool_ctx: ToolContext | None) -> BaseModel | AgentFailure:
    limits = run.settings
    messages: list[dict] = [{"role": "system", "content": spec.system_prompt},
                            {"role": "user", "content": user_message}]
    schemas = [t.openai_schema() for t in spec.tools] or None
    llm_calls = tool_calls = 0

    try:
        # ---- 1. tool-calling phase -------------------------------------------------
        while True:
            can_use_tools = (bool(spec.tools) and tool_calls < limits.max_tool_calls_per_agent
                             and llm_calls < limits.max_llm_calls_per_agent - 1)
            if spec.tools and not can_use_tools:
                messages.append({"role": "user",
                                 "content": "Tool budget used up. Reply now with the final JSON object only."})
            resp = run.llm.chat(agent=spec.name, messages=messages, tools=schemas, allow_tools=can_use_tools)
            llm_calls += 1
            if not (resp.tool_calls and can_use_tools):
                break  # the model answered (or must answer now)
            messages.append(resp.as_message())
            for call in resp.tool_calls:
                if tool_calls >= limits.max_tool_calls_per_agent:
                    result = json.dumps({"error": "tool budget exhausted; call not executed"})
                else:
                    tool_calls += 1
                    result = execute_tool(spec, call, run, tool_ctx)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result})

        # ---- 2. validate the final answer (one repair attempt) ----------------------
        for attempt in (1, 2):
            try:
                return spec.output_model.model_validate(spec.postprocess(extract_json(resp.content)))
            except (ValueError, ValidationError) as exc:
                if attempt == 2:
                    run.calllog.trace(spec.name, f"output still invalid after repair: {_short(exc)}", "red")
                    return AgentFailure(agent=spec.name, error=f"invalid output after repair: {_short(exc)}",
                                        attempts=llm_calls)
                run.calllog.trace(spec.name, f"invalid output, asking for a repair: {_short(exc)}", "yellow")
                resp = _repair(spec, messages, resp, exc, run, schemas)
                llm_calls += 1
    except LLMError as exc:
        run.calllog.trace(spec.name, f"LLM unavailable: {_short(exc)}", "red")
        return AgentFailure(agent=spec.name, error=str(exc), attempts=exc.attempts)
    raise AssertionError("unreachable")


def _repair(spec: AgentSpec, messages: list[dict], bad: LLMResponse, exc: Exception,
            run: RunContext, schemas: list[dict] | None) -> LLMResponse:
    messages.append({"role": "assistant", "content": bad.content or ""})
    messages.append({"role": "user", "content": f"Your reply is not valid: {_short(exc, 800)}\n"
                                                "Reply with the corrected JSON object only."})
    return run.llm.chat(agent=spec.name, messages=messages, tools=schemas, allow_tools=False)


def execute_tool(spec: AgentSpec, call: ToolCall, run: RunContext, tool_ctx: ToolContext | None) -> str:
    """Validate arguments, run the tool with retries + logging, return compact JSON text for the LLM."""
    tool = next((t for t in spec.tools if t.name == call.name), None)
    if tool is None or tool_ctx is None:
        run.calllog.log(agent=spec.name, call_type="tool", name=call.name, latency_ms=0, status="error",
                        error="tool not available to this agent")
        return json.dumps({"error": f"tool {call.name!r} is not available to you"})
    try:
        args = tool.input_model.model_validate_json(call.arguments or "{}")
    except ValidationError as exc:
        run.calllog.log(agent=spec.name, call_type="tool", name=tool.name, latency_ms=0, status="error",
                        error=f"invalid arguments: {_short(exc)}")
        return json.dumps({"error": f"invalid arguments: {_short(exc)}"})

    started = time.monotonic()
    label = f"{tool.name}({_short_args(args)})"
    try:
        result = run.calllog.call(agent=spec.name, call_type="tool", name=tool.name,
                                  fn=lambda: tool.fn(args, tool_ctx), is_retryable=is_retryable_http,
                                  attempts=run.settings.retry_attempts)
    except ToolError as exc:
        run.calllog.trace(spec.name, f"{label} ✗ {exc}", "yellow")
        return json.dumps({"error": str(exc)})
    except Exception as exc:  # noqa: BLE001 - data source down etc.: tell the LLM, keep going
        run.calllog.trace(spec.name, f"{label} ✗ {type(exc).__name__}: {_short(exc)}", "red")
        return json.dumps({"error": f"{tool.name} failed: {type(exc).__name__}: {_short(exc)}"})
    run.calllog.trace(spec.name, f"{label} ✓ {int((time.monotonic() - started) * 1000)} ms", "green")
    return truncate_result(result)


def _short(exc: Exception | str, limit: int = 240) -> str:
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[:limit] + "…"


def _short_args(args: BaseModel, limit: int = 90) -> str:
    text = ", ".join(f"{k}={v}" for k, v in args.model_dump(exclude_none=True, mode="json").items())
    return text if len(text) <= limit else text[:limit] + "…"
