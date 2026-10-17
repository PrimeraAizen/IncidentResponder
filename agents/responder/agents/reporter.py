"""Reporter: writes the final incident report and recommended actions.

Input: plan, evidence, hypotheses, verdicts (+ the hypotheses they judged, failures). Output: IncidentReport (+ Markdown file).
Tools (1): write_report - run by this module's code once the report is valid.
Done when: the report is saved in reports/.

Division of labour:
- final_decision() (code) fixes the facts: which hypothesis is the result, the
  confidence after verification/degradation, the report status and the known limitations;
- the LLM only WRITES: summary, timeline, recommended actions, evidence references;
- if the LLM fails, or the run is out of time, a plain template report is written instead,
  so a run always ends with a report.
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
    verdict: Verdict | None = None  # the Verifier's verdict about `hypothesis` (None if never verified)


def _key(h: Hypothesis) -> tuple[str, str]:
    return h.root_cause_service, h.fault_type


def final_decision(hypotheses: HypothesisSet | None, verdicts: list[Verdict], tested: list[Hypothesis],
                   failures: list[AgentFailure], stopped_early: str | None = None) -> FinalDecision:
    """Pure rules (unit-tested). `tested[i]` is the hypothesis that `verdicts[i]` judged.

    1. The Diagnostician's latest top hypothesis is the result, unless the Verifier rejected it.
    2. If it was rejected, the result is the most recent hypothesis the Verifier did not reject.
    3. If the Verifier rejected every hypothesis it tested, its checks did not settle the question:
       the result is the hypothesis the Diagnostician was most confident in, marked partial.
    Confidence: confirmed +0.1, inconclusive x0.8, unverified x0.8, rejected x0.5, each failed specialist x0.8.
    `stopped_early` (time or step limit) makes the report partial and says why.
    """
    limitations = [f"{f.agent} failed ({f.error[:120]}); its data is missing" for f in failures]
    if stopped_early:
        limitations.insert(0, f"the investigation was stopped early: {stopped_early}")
    top = hypotheses.hypotheses[0] if hypotheses and hypotheses.hypotheses else None
    if top is None and not tested:
        return FinalDecision(None, "failed", 0.0, limitations + ["no root-cause hypothesis was produced"])

    judged = list(zip(tested, verdicts))
    verdict_of_top = next((v for h, v in reversed(judged) if top and _key(h) == _key(top)), None)
    not_rejected = [(h, v) for h, v in judged if v.status != "rejected"]
    status = "complete"
    if top is not None and verdict_of_top is None:              # never verified (time limit, verifier failure)
        chosen, verdict = top, None
        confidence, status = top.confidence * 0.8, "partial"
        limitations.append("the final hypothesis was not independently verified")
    elif top is not None and verdict_of_top.status != "rejected":
        chosen, verdict = top, verdict_of_top
    elif not_rejected:                                         # rule 2
        chosen, verdict = not_rejected[-1]
    else:                                                      # rule 3: everything tested was rejected
        chosen = max(reversed(tested), key=lambda h: h.confidence)  # ties: the most recent one
        verdict = next(v for h, v in reversed(judged) if _key(h) == _key(chosen))
        limitations.append("the Verifier rejected every hypothesis it tested; reporting the Diagnostician's "
                           f"most confident one. Last objection: {verdicts[-1].reasoning[:160]}")
    if verdict is not None:
        if verdict.status == "confirmed":
            confidence = min(1.0, chosen.confidence + 0.1)
        elif verdict.status == "inconclusive":
            confidence = chosen.confidence * 0.8
            limitations.append(f"verification was inconclusive: {verdict.reasoning[:160]}")
        else:
            confidence, status = chosen.confidence * 0.5, "partial"
    specialist_failures = [f for f in failures if f.agent in ("log_analyst", "metrics_analyst", "change_investigator")]
    if specialist_failures:
        confidence *= 0.8 ** len(specialist_failures)
        status = "partial"
    if stopped_early:
        status = "partial"
    return FinalDecision(chosen, status, round(confidence, 2), limitations, verdict)


def run(plan: InvestigationPlan, bundles: list[EvidenceBundle], hypotheses: HypothesisSet | None,
        verdicts: list[Verdict], tested: list[Hypothesis], failures: list[AgentFailure],
        ctx: RunContext, stopped_early: str | None = None) -> tuple[IncidentReport, dict, AgentFailure | None]:
    if stopped_early is None and ctx.time_left() <= 0:
        stopped_early = f"run time limit reached (RUN_TIMEOUT_S={ctx.settings.run_timeout_s})"
    decision = final_decision(hypotheses, verdicts, tested, failures, stopped_early)
    verdict = decision.verdict
    evidence = [e for b in bundles for e in b.evidence] + (verdict.checks if verdict else [])
    known_ids = {e.evidence_id for e in evidence}

    def fix(data: dict) -> dict:
        return _fill(data, plan, decision, known_ids)

    failure = None
    if decision.hypothesis is None or stopped_early:  # nothing to write about, or no time left: template
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
