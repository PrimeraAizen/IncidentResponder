"""Tool plumbing shared by every tool.

A Tool = name + description + Pydantic input model + Python function.
The input model is used twice:
  1. its JSON schema is sent to the LLM (native function calling);
  2. the arguments the LLM sends back are validated against it.
Tools return compact JSON-able dicts (summaries, not raw data) to keep prompts small.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

import httpx
from pydantic import BaseModel

from responder.config import Settings

MAX_RESULT_CHARS = 6000  # ~1.5k tokens: hard cap on what one tool call adds to a prompt


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    fn: Callable[[Any, "ToolContext"], dict]

    def openai_schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name,
            "description": self.description,
            "parameters": _simplify_schema(self.input_model.model_json_schema()),
        }}


@dataclass
class ToolContext:
    """What a tool may know besides its arguments: endpoints and the investigation window.

    Time arguments are optional for every tool; when the LLM omits them, the
    task's window is used. This keeps tool calls short and avoids date mistakes.
    """
    settings: Settings
    http: httpx.Client
    window_start: datetime
    window_end: datetime
    incident_start: datetime  # alert.started_at

    def window(self, start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
        return start or self.window_start, end or self.window_end

    def baseline_and_incident(self) -> tuple[tuple[datetime, datetime], tuple[datetime, datetime]]:
        """Default comparison windows around the alert time A.

        Alerts fire some time after the fault starts (rule `for:` clause), so the
        incident window starts 30 s before A and the baseline is the 5 minutes
        ending 60 s before A. Short baselines keep older, unrelated events out.
        """
        baseline_end = self.incident_start - timedelta(seconds=60)
        baseline_start = max(self.window_start, baseline_end - timedelta(minutes=5))
        return (baseline_start, baseline_end), (self.incident_start - timedelta(seconds=30), self.window_end)


class ToolError(Exception):
    """A problem the LLM can fix (bad query, unknown label). Not retried; returned as {"error": ...}."""


class NoArgs(BaseModel):
    """Input model for tools without parameters."""


def is_retryable_http(exc: BaseException) -> bool:
    """Retry network errors/timeouts and 5xx responses; never 4xx (those are our fault)."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


def truncate_result(result: dict) -> str:
    """Serialize a tool result for the LLM, cutting it to MAX_RESULT_CHARS."""
    text = json.dumps(result, default=str, ensure_ascii=False)
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + ' …[truncated: narrow the query to see more]'


def _simplify_schema(schema: Any) -> Any:
    """Make Pydantic's JSON schema friendlier to all providers: drop titles, turn
    Optional[X] (anyOf X|null) into plain X (optional = not in "required")."""
    if isinstance(schema, dict):
        any_of = schema.get("anyOf")
        if any_of and len(any_of) == 2 and {"type": "null"} in any_of:
            inner = next(s for s in any_of if s != {"type": "null"})
            schema = {**{k: v for k, v in schema.items() if k != "anyOf"}, **inner}
        return {k: _simplify_schema(v) for k, v in schema.items()
                if k != "title" and not (k == "default" and v is None)}
    if isinstance(schema, list):
        return [_simplify_schema(s) for s in schema]
    return schema
