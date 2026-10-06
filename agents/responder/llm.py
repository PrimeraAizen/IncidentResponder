"""LLM access layer: one interface, two providers.

- OpenAICompatLLM: any OpenAI-compatible Chat Completions endpoint (OpenAI,
  Gemini, OpenRouter, local Ollama) with native function calling, temperature 0.
- FakeLLM: deterministic canned outputs per agent (LLM_PROVIDER=fake), so tests
  can exercise routing, limits, retries and logging without an API key.

Both return the same normalized LLMResponse. Every call goes through
CallLogger.call(), which retries transient errors with exponential backoff
(tenacity) and writes one log line per attempt.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import openai

from responder.calllog import CallLogger
from responder.config import Settings


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string; validated later by the tool's input model


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0

    def as_message(self) -> dict[str, Any]:
        """The assistant message to append to the conversation history."""
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments}}
                for tc in self.tool_calls
            ]
        return msg


class LLMError(RuntimeError):
    """The LLM could not be reached / answered with an error after all retries."""


class TransientLLMError(RuntimeError):
    """A temporary failure that is worth retrying (used by FakeLLM to simulate outages)."""


class LLM:
    """Base class: retry + logging around a provider-specific _complete()."""

    model_name = "llm"

    def __init__(self, calllog: CallLogger, attempts: int = 3):
        self.calllog = calllog
        self.attempts = attempts

    def chat(self, *, agent: str, messages: list[dict], tools: list[dict] | None = None,
             allow_tools: bool = True) -> LLMResponse:
        """One chat completion. `allow_tools=False` forces a text (final JSON) answer."""
        try:
            return self.calllog.call(
                agent=agent, call_type="llm", name=self.model_name,
                fn=lambda: self._complete(agent, messages, tools, allow_tools),
                is_retryable=self._is_retryable, attempts=self.attempts,
                usage=lambda r: (r.tokens_in, r.tokens_out),
            )
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"LLM call failed ({self.model_name}): {exc}") from exc

    def _complete(self, agent: str, messages: list[dict], tools: list[dict] | None,
                  allow_tools: bool) -> LLMResponse:
        raise NotImplementedError

    def _is_retryable(self, exc: BaseException) -> bool:
        return isinstance(exc, TransientLLMError)


class OpenAICompatLLM(LLM):
    def __init__(self, settings: Settings, calllog: CallLogger):
        super().__init__(calllog, settings.retry_attempts)
        self.model_name = settings.llm_model
        # max_retries=0: tenacity (in CallLogger) is the only retry layer, so every attempt is logged.
        self.client = openai.OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key,
                                    timeout=settings.llm_timeout_s, max_retries=0)

    def _complete(self, agent, messages, tools, allow_tools) -> LLMResponse:
        kwargs: dict[str, Any] = {"model": self.model_name, "messages": messages, "temperature": 0}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto" if allow_tools else "none"
        resp = self.client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        calls = [
            ToolCall(id=tc.id or f"call_{uuid.uuid4().hex[:8]}", name=tc.function.name,
                     arguments=tc.function.arguments or "{}")
            for tc in (msg.tool_calls or [])
        ]
        usage = resp.usage
        return LLMResponse(msg.content, calls,
                           usage.prompt_tokens if usage else 0, usage.completion_tokens if usage else 0)

    def _is_retryable(self, exc: BaseException) -> bool:
        return isinstance(exc, (openai.APIConnectionError, openai.RateLimitError,
                                openai.InternalServerError, TransientLLMError))


# --------------------------------------------------------------------------- fake
# Canned outputs are clearly marked "[fake]" so they are never mistaken for real reasoning.
FAKE_SCRIPTS: dict[str, dict[str, Any]] = {
    "log_analyst": {
        "tool_calls": [("error_pattern_summary", {})],
        "final": {"status": "ok", "notes": "[fake] canned output", "evidence": [{
            "evidence_id": "x", "source": "logs", "service": "gateway",
            "signal": "[fake] repeated 'slow upstream response' warnings with upstream=payment",
            "strength": 0.8, "raw_ref": "error_pattern_summary()"}]},
    },
    "metrics_analyst": {
        "tool_calls": [("targets_health", {}), ("zscore_anomaly", {
            "promql": "histogram_quantile(0.95, sum by (le, service, target) "
                      "(rate(upstream_request_duration_seconds_bucket[1m])))"})],
        "final": {"status": "ok", "notes": "[fake] canned output", "evidence": [{
            "evidence_id": "x", "source": "metrics", "service": "gateway",
            "signal": "[fake] p95 latency of gateway→payment calls far above baseline",
            "strength": 0.9, "raw_ref": "zscore_anomaly(upstream p95)"}]},
    },
    "diagnostician": {
        "tool_calls": [("dependency_graph", {})],
        "final": {"hypotheses": [{
            "hypothesis_id": "h1", "root_cause_service": "payment", "fault_type": "latency",
            "explanation": "[fake] gateway waits on slow payment calls",
            "supporting_evidence": ["logs-1", "metrics-1"], "suspect_commit": None, "confidence": 0.7}]},
    },
}


class FakeLLM(LLM):
    """Replays a two-step script per agent: canned tool calls, then canned final JSON.

    The tool calls are executed for real by the runtime, so tool code, budgets and
    logging are exercised. `fail_agents` raise TransientLLMError (tests retries and
    degradation); `invalid_json_agents` first answer with broken JSON (tests the repair call).
    """

    model_name = "fake"

    def __init__(self, calllog: CallLogger, attempts: int = 3, scripts: dict | None = None,
                 fail_agents: set[str] | None = None, invalid_json_agents: set[str] | None = None):
        super().__init__(calllog, attempts)
        self.scripts = scripts if scripts is not None else FAKE_SCRIPTS
        self.fail_agents = fail_agents or set()
        self.invalid_json_agents = set(invalid_json_agents or ())

    def _complete(self, agent, messages, tools, allow_tools) -> LLMResponse:
        if agent in self.fail_agents:
            raise TransientLLMError("simulated provider outage")
        script = self.scripts.get(agent, {"tool_calls": [], "final": {}})
        tokens_in = sum(len(str(m.get("content") or "")) for m in messages) // 4
        tools_done = any(m.get("role") == "tool" for m in messages)
        if tools and allow_tools and script["tool_calls"] and not tools_done:
            calls = [ToolCall(id=f"fake_{i}", name=name, arguments=json.dumps(args))
                     for i, (name, args) in enumerate(script["tool_calls"])]
            return LLMResponse(None, calls, tokens_in, 20)
        if agent in self.invalid_json_agents:
            self.invalid_json_agents.discard(agent)  # only the first answer is broken
            return LLMResponse("this is not JSON", [], tokens_in, 5)
        content = json.dumps(script["final"])
        return LLMResponse(content, [], tokens_in, len(content) // 4)


def make_llm(settings: Settings, calllog: CallLogger) -> LLM:
    """Create the provider selected by LLM_PROVIDER."""
    if settings.llm_provider == "fake":
        return FakeLLM(calllog, attempts=settings.retry_attempts)
    if settings.llm_provider == "openai":
        if not settings.llm_api_key:
            raise LLMError("LLM_API_KEY is not set. Copy .env.example to .env and configure a provider "
                           "(or use LLM_PROVIDER=fake for an offline dry run).")
        return OpenAICompatLLM(settings, calllog)
    raise LLMError(f"Unknown LLM_PROVIDER={settings.llm_provider!r}; use 'openai' or 'fake'.")


def extract_json(text: str | None) -> dict[str, Any]:
    """Parse the JSON object in a model reply (tolerates ```json fences and prose around it)."""
    if not text or not text.strip():
        raise ValueError("empty reply; expected a JSON object")
    body = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", body, re.S)
    if fenced:
        body = fenced.group(1).strip()
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in reply")
    data = json.loads(body[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("reply JSON is not an object")
    return data
