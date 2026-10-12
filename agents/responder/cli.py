"""Command-line entry point.

    responder investigate --alert alerts/s01.json

Loads the alert, runs the agent graph, prints the evidence and hypotheses,
and points to the call log (logs/run_<id>.jsonl) and the saved result.
Exit code: 0 = hypotheses produced, 1 = no hypotheses (total failure), 2 = bad input/config.
"""

from __future__ import annotations

from pathlib import Path

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from responder import graph
from responder.calllog import summarize
from responder.config import Settings
from responder.llm import LLMError
from responder.schemas import Alert

app = typer.Typer(add_completion=False, help="Multi-agent incident root-cause analysis.")


@app.callback()
def main() -> None:
    """Incident Responder: investigates an alert with a team of LLM agents."""


@app.command()
def investigate(alert: Path = typer.Option(..., "--alert", "-a", exists=True, dir_okay=False,
                                           help="Alert JSON file (e.g. written by `make scenario`).")) -> None:
    """Investigate one alert: plan, evidence, hypotheses, verification, report."""
    console = Console()
    settings = Settings.from_env()
    try:
        alert_obj = Alert.model_validate_json(alert.read_text(encoding="utf-8"))
    except ValidationError as exc:
        console.print(f"[red]✗ Invalid alert file {alert}:[/]\n{escape(str(exc))}")
        raise typer.Exit(2)

    console.rule(f"[bold]{escape(alert_obj.alert_name)}[/] on [bold]{escape(alert_obj.service)}[/] — {escape(alert_obj.summary)}")
    model = "fake (canned outputs)" if settings.llm_provider == "fake" else settings.llm_model
    console.print(f"LLM: {model}   alert started at {alert_obj.started_at:%Y-%m-%d %H:%M:%S} UTC\n")
    try:
        result = graph.investigate(alert_obj, settings, console=console)
    except LLMError as exc:
        console.print(f"[red]✗ {escape(str(exc))}[/]")
        raise typer.Exit(2)

    _print_result(console, result)
    raise typer.Exit(0 if result.report and result.report.status != "failed" else 1)


def _print_result(console: Console, result: graph.InvestigationResult) -> None:
    console.print()
    evidence = Table(title="Evidence from specialists", show_lines=True, expand=True)
    for col, kw in [("id", {"no_wrap": True}), ("service", {}), ("signal", {"ratio": 1}), ("strength", {"justify": "right"})]:
        evidence.add_column(col, **kw)
    for bundle in result.bundles:
        if bundle.status == "failed":
            evidence.add_row(f"[red]{bundle.agent}[/]", "—", f"[red]FAILED: {escape(bundle.notes)}[/]", "—")
        elif not bundle.evidence:
            evidence.add_row(bundle.agent, "—", f"no anomalies found. {escape(bundle.notes)}", "—")
        for e in bundle.evidence:
            evidence.add_row(e.evidence_id, escape(e.service), escape(e.signal), f"{e.strength:.2f}")
    console.print(evidence)

    if result.hypotheses:
        hyps = Table(title="Root-cause hypotheses (ranked)", show_lines=True, expand=True)
        for col, kw in [("#", {}), ("root cause", {"style": "bold"}), ("fault type", {}),
                        ("confidence", {"justify": "right"}), ("evidence", {}), ("explanation", {"ratio": 1})]:
            hyps.add_column(col, **kw)
        for h in result.hypotheses.hypotheses:
            hyps.add_row(h.hypothesis_id, escape(h.root_cause_service), h.fault_type, f"{h.confidence:.2f}",
                         ", ".join(h.supporting_evidence), escape(h.explanation))
        console.print(hyps)
    else:
        console.print("[red]✗ The Diagnostician produced no hypotheses (see failures above and the call log).[/]")

    for v in result.verdicts:
        color = {"confirmed": "green", "rejected": "red"}.get(v.status, "yellow")
        console.print(f"[bold]Verifier[/] on {v.hypothesis_id}: [{color}]{v.status}[/] — {escape(v.reasoning)}")
        for c in v.checks:
            console.print(f"   {c.evidence_id}: {escape(c.signal)}", highlight=False)

    if result.report:
        r = result.report
        color = {"complete": "green", "partial": "yellow"}.get(r.status, "red")
        console.print()
        console.rule(f"Incident report [{color}]{r.status}[/]")
        console.print(f"[bold]Root cause:[/] {escape(r.root_cause_service)} / {r.fault_type}   "
                      f"[bold]confidence[/] {r.confidence:.2f}"
                      + (f"   [bold]suspect commit[/] {escape(r.suspect_commit)}" if r.suspect_commit else ""))
        console.print(escape(r.summary))
        for action in r.recommended_actions:
            console.print(f"  → {escape(action)}")
        for item in r.limitations:
            console.print(f"  [yellow]limitation:[/] {escape(item)}")
        console.print(f"Report:   {result.report_paths.get('md_path', result.report_paths)}")

    for f in result.failures:
        console.print(f"[yellow]! {f.agent} failed after {f.attempts} attempt(s): {escape(f.error)}[/]")

    calls = summarize(result.log_path)
    total = sum(c["llm"] + c["tool"] for c in calls.values()) or 1
    shares = ", ".join(f"{agent} {c['llm']}+{c['tool']} ({(c['llm'] + c['tool']) / total:.0%})"
                       for agent, c in sorted(calls.items()))
    console.print(f"\nCalls (llm+tool): {shares}")
    console.print(f"Call log: {result.log_path}\nResult:   {result.result_path}")


if __name__ == "__main__":
    app()
