"""Orchestrator: turns the alert into an investigation plan and assigns tasks. No analysis.

Input: Alert. Output: InvestigationPlan (window, focus services, one Task per specialist).
Tools: none. Exactly 1 LLM call (+1 repair if its JSON is invalid).
Done when: a valid plan exists. If the LLM fails, the deterministic planner's plan is used.

The LLM decides only planning choices: how far back to look, which services to
focus on, and a tailored objective per specialist. Ids, exact timestamps and
the list of specialists are filled in by code.
"""

from __future__ import annotations

from datetime import datetime, timezone

from responder.agents.common import load_prompt
from responder.agents.planner import SPECIALIST_OBJECTIVES, build_plan
from responder.runtime import AgentSpec, RunContext, run_agent
from responder.schemas import AgentFailure, Alert, InvestigationPlan
from responder.tools.topology import load_topology

NAME = "orchestrator"
TOOLS: list = []
MIN_LOOKBACK_MIN, MAX_LOOKBACK_MIN = 5, 30


def run(alert: Alert, incident_id: str, ctx: RunContext) -> tuple[InvestigationPlan, AgentFailure | None]:
    now = datetime.now(timezone.utc)
    components = load_topology()

    def fix(data: dict) -> dict:
        lookback = int(data.get("lookback_minutes") or 10)
        lookback = min(MAX_LOOKBACK_MIN, max(MIN_LOOKBACK_MIN, lookback))
        focus = [s for s in data.get("focus_services") or [] if s in components]
        if alert.service in components and alert.service not in focus:
            focus.insert(0, alert.service)
        objectives = {t.get("agent"): t.get("objective") for t in data.get("tasks") or []
                      if t.get("agent") in SPECIALIST_OBJECTIVES and t.get("objective")}
        plan = build_plan(alert, incident_id, now, lookback_min=lookback, focus=focus or None, objectives=objectives)
        return plan.model_dump()

    spec = AgentSpec(name=NAME, system_prompt=load_prompt(NAME), tools=TOOLS,
                     output_model=InvestigationPlan, postprocess=fix)
    result = run_agent(spec, _message(alert, components, now), ctx, tool_ctx=None)
    if isinstance(result, AgentFailure):
        ctx.calllog.trace(NAME, "using the deterministic fallback plan", "yellow")
        return build_plan(alert, incident_id, now), result
    ctx.calllog.trace(NAME, f"plan: window {result.window_start:%H:%M:%S}–{result.window_end:%H:%M:%S} UTC, "
                            f"focus: {', '.join(result.focus_services)}", "bold")
    return result, None


def _message(alert: Alert, components: dict[str, dict], now: datetime) -> str:
    deps = "\n".join(f"- {name} ({c['kind']}): {c['description']} Calls: {', '.join(c['depends_on']) or 'nothing'}."
                     for name, c in components.items())
    return (f"Alert:\n{alert.model_dump_json(indent=1)}\n\n"
            f"Current time: {now:%Y-%m-%dT%H:%M:%SZ}\n\n"
            f"System components:\n{deps}")
