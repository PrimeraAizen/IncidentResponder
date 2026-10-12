"""write_report tool (Reporter): saves the incident report as reports/<incident_id>.md and .json.

The only tool that writes anything. The Markdown is rendered by code from the
validated IncidentReport, so the file always matches the structured result.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from responder.schemas import Evidence, IncidentReport, Verdict
from responder.tools.base import Tool, ToolContext


class WriteReportArgs(BaseModel):
    report: IncidentReport
    evidence: list[Evidence] = Field(default_factory=list)  # the evidence the report refers to
    verdict: Verdict | None = None


def write_report(args: WriteReportArgs, ctx: ToolContext) -> dict:
    out = ctx.settings.reports_dir
    out.mkdir(parents=True, exist_ok=True)
    report = args.report
    json_path = out / f"{report.incident_id}.json"
    md_path = out / f"{report.incident_id}.md"
    json_path.write_text(json.dumps(args.model_dump(mode="json"), indent=2, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_markdown(args), encoding="utf-8")
    return {"md_path": str(md_path), "json_path": str(json_path)}


def render_markdown(args: WriteReportArgs) -> str:
    r = args.report
    lines = [
        f"# Incident {r.incident_id}: {r.root_cause_service} / {r.fault_type}",
        "",
        f"**Status:** {r.status} · **Confidence:** {r.confidence:.2f}"
        + (f" · **Suspect commit:** `{r.suspect_commit}`" if r.suspect_commit else ""),
        "",
        "## Summary",
        "",
        r.summary,
        "",
        "## Timeline",
        "",
        *[f"- {item}" for item in r.timeline],
        "",
        "## Recommended actions",
        "",
        *[f"{i}. {action}" for i, action in enumerate(r.recommended_actions, start=1)],
        "",
    ]
    if args.verdict:
        lines += ["## Verification", "", f"Verifier verdict: **{args.verdict.status}**. {args.verdict.reasoning}", ""]
        lines += [f"- {c.signal}" for c in args.verdict.checks] + [""]
    cited = [e for e in args.evidence if e.evidence_id in set(r.evidence_refs)]
    if cited:
        lines += ["## Evidence", "", "| id | source | service | signal |", "|---|---|---|---|"]
        lines += [f"| {e.evidence_id} | {e.source} | {e.service} | {e.signal.replace('|', '/')} |" for e in cited]
        lines.append("")
    if r.limitations:
        lines += ["## Limitations", "", *[f"- {item}" for item in r.limitations], ""]
    return "\n".join(lines)


WRITE_REPORT = Tool("write_report", "Save the incident report (Markdown + JSON) to the reports directory.",
                    WriteReportArgs, write_report)
