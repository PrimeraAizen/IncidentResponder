"""Verifier: independently tests the top hypothesis against fresh queries; accepts or rejects it.

Input: the top Hypothesis + the InvestigationPlan (alert, window). It does NOT
see the specialists' evidence: it designs and runs its own checks, so it
cannot simply re-confirm what the Diagnostician already believed.
Output: Verdict (confirmed / rejected / inconclusive) with the checks performed.
Tools (3): prom_query, loki_query, health_check.
Done when: a verdict with ≥1 check.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from responder.agents.common import load_prompt, tool_context
from responder.runtime import AgentSpec, RunContext, run_agent
from responder.schemas import AgentFailure, Hypothesis, InvestigationPlan, Verdict
from responder.tools.health import HEALTH_CHECK
from responder.tools.loki import LOKI_QUERY
from responder.tools.prometheus import PROM_QUERY

NAME = "verifier"
TOOLS = [PROM_QUERY, LOKI_QUERY, HEALTH_CHECK]


def run(hypothesis: Hypothesis, plan: InvestigationPlan, ctx: RunContext) -> Verdict | AgentFailure:
    def fix(data: dict) -> dict:
        data["hypothesis_id"] = hypothesis.hypothesis_id
        checks = data.get("checks") or []
        if not checks:
            raise ValueError("a verdict needs at least one check you actually performed")
        for i, check in enumerate(checks, start=1):
            check["evidence_id"], check["source"] = f"verify-{i}", "verification"
        return data

    spec = AgentSpec(name=NAME, system_prompt=load_prompt(NAME), tools=TOOLS, output_model=Verdict, postprocess=fix)
    ctx.calllog.trace(NAME, f"testing: {hypothesis.root_cause_service} / {hypothesis.fault_type}", "dim")
    # Fresh data: the checks run "now", at the end of a window that may have been planned minutes ago.
    task = plan.tasks[0].model_copy(update={"window_end": max(plan.window_end, datetime.now(timezone.utc))})
    result = run_agent(spec, _message(hypothesis, plan), ctx, tool_context(ctx, task, plan.alert))
    if isinstance(result, Verdict):
        ctx.calllog.trace(NAME, f"verdict: {result.status} ({len(result.checks)} check(s))", "bold")
    return result


def baseline_offset_minutes(alert_started_at: datetime, now: datetime) -> int:
    """A PromQL `offset` that points safely before the incident: minutes since the alert, plus 2."""
    return math.ceil((now - alert_started_at).total_seconds() / 60) + 2


def _message(hypothesis: Hypothesis, plan: InvestigationPlan) -> str:
    claim = hypothesis.model_dump(include={"root_cause_service", "fault_type", "explanation", "suspect_commit"})
    now = datetime.now(timezone.utc)
    offset = baseline_offset_minutes(plan.alert.started_at, now)
    return (f"Hypothesis to test:\n{claim}\n\n"
            f"Alert:\n{plan.alert.model_dump_json()}\n\n"
            f"The alert fired at {plan.alert.started_at:%Y-%m-%dT%H:%M:%SZ}; it is now {now:%Y-%m-%dT%H:%M:%SZ}. "
            f"Components in scope: {', '.join(plan.focus_services)}.\n"
            f"Baseline for comparisons: `offset {offset}m` points to a moment BEFORE the incident, "
            f"e.g. `X / (X offset {offset}m)` or the same query with `[1m] offset {offset}m`.")
