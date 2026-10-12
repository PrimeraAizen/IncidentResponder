"""Evaluate the agents end to end on one or more scenarios (SPEC 9).

For each scenario and repeat:
    reset → noise deploys → load → warm-up → fault → observe → alert
    → investigate (all 7 agents) → score against the ground truth → save → next
and a final reset (teardown) at the end.

    uv run --project agents python eval/run_scenario.py s01 --repeats 3
    uv run --project agents python eval/run_scenario.py s01 s02 s03 s04 s05 s06

Each run is saved to eval/results/<timestamp>/<scenario>_<n>.json.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # run as a script: make the `chaos` and `eval` packages importable

import typer  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

from chaos import runner  # noqa: E402
from chaos.cli import say, wait  # noqa: E402
from chaos.scenario import load_scenario  # noqa: E402
from eval.scoring import score  # noqa: E402
from responder import graph  # noqa: E402
from responder.config import Settings  # noqa: E402
from responder.schemas import Alert  # noqa: E402

console = Console()


def run_once(scenario_id: str, repeat: int, out_dir: Path, settings: Settings) -> dict:
    scn = load_scenario(scenario_id)
    console.rule(f"[bold]{scn.id}[/] — run {repeat}")
    run = runner.run_until_alert(scn, say, wait)

    started = time.monotonic()
    result = graph.investigate(Alert.model_validate(run.alert), settings, console=console)
    duration_s = round(time.monotonic() - started, 1)

    commits = [e["commit"] for e in run.scenario_deploys]
    verdict = score(scn.expected, result.report, commits)
    calls = [json.loads(line) for line in result.log_path.read_text().splitlines()]
    report = result.report
    record = {
        "scenario": scn.id, "repeat": repeat, "run_id": result.run_id, "incident_id": result.incident_id,
        "expected": scn.expected, "scenario_commits": commits,
        "symptoms_ok": run.symptoms_ok,
        "symptoms": [{"check": c.description or c.promql, "value": v, "ok": ok} for c, v, ok in run.symptoms],
        "alert": run.alert,
        "report": report.model_dump(mode="json") if report else None,
        "verdicts": [v.status for v in result.verdicts],
        "score": verdict,
        "investigation_s": duration_s,
        "llm_calls": sum(1 for c in calls if c["call_type"] == "llm" and c["status"] != "retry"),
        "tool_calls": sum(1 for c in calls if c["call_type"] == "tool" and c["status"] != "retry"),
        "tokens_in": sum(c["tokens_in"] or 0 for c in calls),
        "tokens_out": sum(c["tokens_out"] or 0 for c in calls),
        "call_log": str(result.log_path), "report_md": result.report_paths.get("md_path"),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{scn.id}_{repeat}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")

    mark = "[green]✓ correct[/]" if verdict["correct"] else "[red]✗ wrong[/]"
    found = f"{report.root_cause_service} / {report.fault_type}" if report else "no report"
    console.print(f"{mark}  {scn.id}: expected {scn.expected['root_cause_service']} / {scn.expected['fault_type']}, "
                  f"got {found}" + (f", commit {'✓' if verdict['commit_ok'] else '✗'}" if verdict["commit_expected"] else ""))
    return record


def print_summary(records: list[dict]) -> None:
    table = Table(title="Evaluation results")
    for col in ("scenario", "run", "expected", "reported", "verdicts", "correct", "commit", "time s", "llm", "tools"):
        table.add_column(col)
    for r in records:
        rep = r["report"] or {}
        table.add_row(r["scenario"], str(r["repeat"]),
                      f"{r['expected']['root_cause_service']} / {r['expected']['fault_type']}",
                      f"{rep.get('root_cause_service')} / {rep.get('fault_type')}", ",".join(r["verdicts"]) or "-",
                      "✅" if r["score"]["correct"] else "❌",
                      {True: "✅", False: "❌", None: "-"}[r["score"]["commit_ok"]],
                      str(r["investigation_s"]), str(r["llm_calls"]), str(r["tool_calls"]))
    console.print(table)
    correct = sum(r["score"]["correct"] for r in records)
    console.print(f"[bold]{correct}/{len(records)} correct[/]")


def main(scenarios: list[str] = typer.Argument(..., help="Scenario ids, e.g. s01 s02"),
         repeats: int = typer.Option(1, help="Runs per scenario."),
         teardown: bool = typer.Option(True, help="Reset to the healthy baseline at the end.")) -> None:
    settings = Settings.from_env()
    out_dir = ROOT / "eval" / "results" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    records = []
    try:
        for scenario_id in scenarios:
            for repeat in range(1, repeats + 1):
                records.append(run_once(scenario_id, repeat, out_dir, settings))
    finally:
        if teardown:
            runner.reset_all(say)
        if records:
            print_summary(records)
            console.print(f"Results: {out_dir}")


if __name__ == "__main__":
    typer.run(main)
