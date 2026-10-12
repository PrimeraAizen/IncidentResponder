"""Helpers shared by the agents: prompt loading and the specialist pattern.

The three specialists (Log, Metrics, Change) work the same way: they get a Task,
use their own small toolset, and return an EvidenceBundle. Only the prompt,
the tools and the evidence id prefix differ, so that logic lives here once.
"""

from __future__ import annotations

import json
from pathlib import Path

from responder.runtime import AgentSpec, RunContext, run_agent
from responder.schemas import AgentFailure, Alert, EvidenceBundle, Task
from responder.tools.base import Tool, ToolContext

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def load_prompt(agent_name: str) -> str:
    return (PROMPTS_DIR / f"{agent_name}.md").read_text(encoding="utf-8")


def tool_context(run: RunContext, task: Task, alert: Alert) -> ToolContext:
    return ToolContext(settings=run.settings, http=run.http, window_start=task.window_start,
                       window_end=task.window_end, incident_start=alert.started_at, docker=run.docker)


def task_message(task: Task, alert: Alert) -> str:
    """The user message a specialist receives: its task and the alert, as JSON."""
    return (
        "Investigate this task.\n"
        f"Alert:\n{alert.model_dump_json(indent=1)}\n"
        f"Task:\n{task.model_dump_json(indent=1)}\n"
        f"The alert fired at {alert.started_at:%Y-%m-%dT%H:%M:%SZ}; the investigation window is "
        f"{task.window_start:%H:%M:%S}–{task.window_end:%H:%M:%S} UTC."
    )


def run_specialist(*, name: str, source: str, id_prefix: str, tools: list[Tool], task: Task,
                   alert: Alert, run: RunContext) -> tuple[EvidenceBundle, AgentFailure | None]:
    """Run one specialist. Never raises: a failure becomes a 'failed' bundle + AgentFailure."""

    def fix(data: dict) -> dict:
        # Fields owned by code, not by the LLM: who produced it, for which task, unique evidence ids.
        data["agent"], data["task_id"] = name, task.task_id
        data.setdefault("status", "ok")
        for i, item in enumerate(data.get("evidence") or [], start=1):
            item["evidence_id"], item["source"] = f"{id_prefix}-{i}", source
        return data

    spec = AgentSpec(name=name, system_prompt=load_prompt(name), tools=tools,
                     output_model=EvidenceBundle, postprocess=fix)
    run.calllog.trace(name, f"task: {task.objective}", "dim")
    result = run_agent(spec, task_message(task, alert), run, tool_context(run, task, alert))

    if isinstance(result, AgentFailure):
        bundle = EvidenceBundle(agent=name, task_id=task.task_id, status="failed", evidence=[],
                                notes=f"agent failed: {result.error}")
        return bundle, result
    run.calllog.trace(name, f"done: {len(result.evidence)} evidence item(s), status={result.status}", "bold")
    return result, None


def compact_json(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
