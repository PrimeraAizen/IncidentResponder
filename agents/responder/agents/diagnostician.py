"""Diagnostician: combines all evidence into ranked root-cause hypotheses.

Input: the InvestigationPlan (alert + window) and every EvidenceBundle
(+ the previous Verdict when the Verifier rejected a hypothesis).
Output: HypothesisSet (ranked, ≤3). Tools (1): dependency_graph.
Done when: ≥1 hypothesis that cites evidence ids from the bundles.
"""

from __future__ import annotations

from responder.agents.common import compact_json, load_prompt
from responder.runtime import AgentSpec, RunContext, run_agent
from responder.schemas import AgentFailure, EvidenceBundle, HypothesisSet, InvestigationPlan, Verdict
from responder.tools.base import ToolContext
from responder.tools.topology import DEPENDENCY_GRAPH

NAME = "diagnostician"
TOOLS = [DEPENDENCY_GRAPH]


def run(plan: InvestigationPlan, bundles: list[EvidenceBundle], ctx: RunContext,
        previous_verdict: Verdict | None = None, rejected: list[str] | None = None) -> HypothesisSet | AgentFailure:
    known_ids = {e.evidence_id for b in bundles for e in b.evidence}

    def fix(data: dict) -> dict:
        hypotheses = data.get("hypotheses") or []
        if not hypotheses:
            raise ValueError("at least one hypothesis is required")
        for i, h in enumerate(hypotheses, start=1):
            h["hypothesis_id"] = f"h{i}"
            # Keep only references to evidence that really exists (LLMs sometimes invent ids).
            h["supporting_evidence"] = [ref for ref in h.get("supporting_evidence") or [] if ref in known_ids]
        if known_ids and not hypotheses[0]["supporting_evidence"]:
            raise ValueError(f"hypothesis h1 must cite evidence ids from the bundles: {sorted(known_ids)}")
        return data

    spec = AgentSpec(name=NAME, system_prompt=load_prompt(NAME), tools=TOOLS,
                     output_model=HypothesisSet, postprocess=fix)
    tool_ctx = ToolContext(settings=ctx.settings, http=ctx.http, window_start=plan.window_start,
                           window_end=plan.window_end, incident_start=plan.alert.started_at)
    ctx.calllog.trace(NAME, f"combining {sum(len(b.evidence) for b in bundles)} evidence item(s) "
                            f"from {len(bundles)} bundle(s)", "dim")
    result = run_agent(spec, _message(plan, bundles, previous_verdict, rejected or []), ctx, tool_ctx)
    if isinstance(result, HypothesisSet):
        top = result.hypotheses[0]
        ctx.calllog.trace(NAME, f"top hypothesis: {top.root_cause_service} / {top.fault_type} "
                                f"(confidence {top.confidence:.2f})", "bold")
    return result


def _message(plan: InvestigationPlan, bundles: list[EvidenceBundle], verdict: Verdict | None,
             rejected: list[str]) -> str:
    parts = [
        f"Alert: {plan.alert.model_dump_json()}",
        f"Investigation window: {plan.window_start:%Y-%m-%dT%H:%M:%SZ} to {plan.window_end:%Y-%m-%dT%H:%M:%SZ}",
        "Evidence bundles from the specialists:",
        *(compact_json(b.model_dump(mode="json")) for b in bundles),
    ]
    failed = [b.agent for b in bundles if b.status != "ok"]
    if failed:
        parts.append(f"NOTE: these specialists failed or returned partial data: {', '.join(failed)}.")
    if verdict:
        parts.append(f"The Verifier REJECTED your previous top hypothesis ({', '.join(rejected)}): "
                     f"{verdict.model_dump_json()}. Propose a different explanation or lower your confidence.")
    return "\n".join(parts)
