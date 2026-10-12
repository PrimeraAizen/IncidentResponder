"""Reporter: writes the final incident report and recommended actions.

Input: plan, evidence, hypotheses, verdict (+ failures). Output: IncidentReport (+ Markdown file).
Tools (1): write_report - run by this module's code once the report is valid.
Done when: the report is saved in reports/.

Division of labour:
- final_decision() (code) fixes the facts: which hypothesis is the result, the
  confidence after verification/degradation, the report status and the known limitations;
- the LLM only WRITES: summary, timeline, recommended actions, evidence references;
- if the LLM fails, a plain template report is written instead, so a run always ends with a report.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from responder.agents.common import compact_json, load_prompt, tool_context
from responder.runtime import AgentSpec, RunContext, run_agent, run_tool
from responder.schemas import (AgentFailure, Evidence, EvidenceBundle, Hypothesis, HypothesisSet, IncidentReport,
                               InvestigationPlan, Verdict)
from responder.tools.report import WRITE_REPORT, WriteReportArgs

NAME = "reporter"
TOOLS = [WRITE_REPORT]


@dataclass
class FinalDecision:
    hypothesis: Hypothesis | None
    status: str           # complete | partial | failed
    confidence: float
    limitations: list[str] = field(default_factory=list)


def final_decision(hypotheses: HypothesisSet | None, verdict: Verdict | None, verified: Hypothesis | None,
                   failures: list[AgentFailure]) -> FinalDecision:
    """Pure rules (unit-tested): the result is the Diagnostician's latest top hypothesis,
    with confidence adjusted by the verification outcome and by missing data."""
    limitations = [f"{f.agent} failed ({f.error[:120]}); its data is missing" for f in failures]
    if not hypotheses or not hypotheses.hypotheses:
        return FinalDecision(None, "failed", 0.0, limitations + ["no root-cause hypothesis was produced"])

    top = hypotheses.hypotheses[0]
    confidence, status = top.confidence, "complete"
    same = verified is not None and (verified.root_cause_service, verified.fault_type) == (top.root_cause_service, top.fault_type)
    if verdict is None or not same:
        confidence *= 0.8
        status = "partial"
        limitations.append("the final hypothesis was not independently verified")
    elif verdict.status == "confirmed":
        confidence = min(1.0, confidence + 0.1)
    elif verdict.status == "inconclusive":
        confidence *= 0.8
        limitations.append(f"verification was inconclusive: {verdict.reasoning[:160]}")
    else:  # rejected, and the loop limit or loop protection stopped further attempts
        confidence *= 0.5
        status = "partial"
        limitations.append(f"the Verifier rejected this hypothesis and no better one was found: {verdict.reasoning[:160]}")
    specialist_failures = [f for f in failures if f.agent in ("log_analyst", "metrics_analyst", "change_investigator")]
    if specialist_failures:
        confidence *= 0.8 ** len(specialist_failures)
        status = "partial"
    return FinalDecision(top, status, round(confidence, 2), limitations)


def run(plan: InvestigationPlan, bundles: list[EvidenceBundle], hypotheses: HypothesisSet | None,
        verdict: Verdict | None, verified: Hypothesis | None, failures: list[AgentFailure],
        ctx: RunContext) -> tuple[IncidentReport, dict, AgentFailure | None]:
    decision = final_decision(hypotheses, verdict, verified, failures)
    evidence = [e for b in bundles for e in b.evidence] + (verdict.checks if verdict else [])
    known_ids = {e.evidence_id for e in evidence}

    def fix(data: dict) -> dict:
        return _fill(data, plan, decision, known_ids)

    failure = None
    if decision.hypothesis is None:
        report = IncidentReport.model_validate(_fill({}, plan, decision, known_ids))
    else:
        spec = AgentSpec(name=NAME, system_prompt=load_prompt(NAME), tools=[],
                         output_model=IncidentReport, postprocess=fix)
        result = run_agent(spec, _message(plan, decision, evidence, verdict), ctx, tool_ctx=None)
        if isinstance(result, AgentFailure):
            ctx.calllog.trace(NAME, "LLM failed; writing a template report", "yellow")
            failure, report = result, IncidentReport.model_validate(_fill({}, plan, decision, known_ids))
        else:
            report = result

    cited = [e for e in evidence if e.evidence_id in set(report.evidence_refs)]
    paths = run_tool(NAME, WRITE_REPORT, WriteReportArgs(report=report, evidence=cited, verdict=verdict),
                     ctx, tool_context(ctx, plan.tasks[0], plan.alert))
    ctx.calllog.trace(NAME, f"report {report.status}: {report.root_cause_service} / {report.fault_type} "
                            f"(confidence {report.confidence:.2f}) → {paths.get('md_path', paths)}", "bold")
    return report, paths, failure


def _fill(data: dict, plan: InvestigationPlan, d: FinalDecision, known_ids: set[str]) -> dict:
    """Code-owned fields of the report; the LLM's text fields are kept (or defaulted)."""
    h = d.hypothesis
    data.update({
        "incident_id": plan.incident_id, "status": d.status,
        "root_cause_service": h.root_cause_service if h else "unknown",
        "fault_type": h.fault_type if h else "no_fault",
        "confidence": d.confidence,
        "suspect_commit": h.suspect_commit if h else None,
    })
    data.setdefault("summary", (f"{plan.alert.alert_name} on {plan.alert.service}. " +
                                (f"Most likely root cause: {h.root_cause_service} ({h.fault_type}). {h.explanation}"
                                 if h else "The root cause could not be determined.")))
    data.setdefault("timeline", [f"{plan.alert.started_at:%H:%M:%S} alert {plan.alert.alert_name} on {plan.alert.service}"])
    data.setdefault("recommended_actions", ["Investigate manually: see limitations."])
    data["evidence_refs"] = [ref for ref in data.get("evidence_refs") or (h.supporting_evidence if h else [])
                             if ref in known_ids]
    data["limitations"] = d.limitations + [x for x in data.get("limitations") or [] if x not in d.limitations]
    return data


def _message(plan: InvestigationPlan, d: FinalDecision, evidence: list[Evidence], verdict: Verdict | None) -> str:
    h = d.hypothesis
    assert h is not None  # the LLM is only asked to write when there is a diagnosis
    return "\n".join([
        f"Alert: {plan.alert.model_dump_json()}",
        f"FINAL DIAGNOSIS (fixed, do not change): root cause {h.root_cause_service}, fault type {h.fault_type}, "
        f"confidence {d.confidence:.2f}, status {d.status}, suspect commit {h.suspect_commit or 'none'}.",
        f"Diagnostician's explanation: {h.explanation}",
        f"Verifier: {verdict.status + ' - ' + verdict.reasoning if verdict else 'no verdict'}",
        f"Known limitations: {d.limitations or 'none'}",
        "Evidence:",
        *(compact_json(e.model_dump(mode="json", exclude={"raw_ref"})) for e in evidence),
    ])
