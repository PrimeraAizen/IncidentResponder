"""Chaos CLI: run scenarios, simulate deploys, reset to baseline.

    python -m chaos list
    python -m chaos scenario s01     # full run: reset, noise deploys, warm-up, fault, symptom check, alert
    python -m chaos apply s01        # inject only
    python -m chaos deploy order s04_order_promotions.patch
    python -m chaos reset            # back to the healthy baseline
"""

from __future__ import annotations

import json
import time

import typer
import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn

from chaos import deploy_sim, faults, runner
from chaos.scenario import apply_setup, load_scenario, scenario_files

app = typer.Typer(add_completion=False, help="Fault-injection harness (never used by the agents).")
console = Console()
load_dotenv(faults.ROOT / ".env", override=False)


def say(line: str) -> None:
    style = "red" if line.startswith("✗") else "green" if line.startswith("✓") else ""
    console.print(f"[{style}]{escape(line[:1])}[/]{escape(line[1:])}" if style else escape(line), highlight=False)


def wait(label: str, seconds: int) -> None:
    with Progress(TextColumn("  {task.description}"), BarColumn(), TimeRemainingColumn(), console=console) as bar:
        task = bar.add_task(label, total=seconds)
        for _ in range(seconds):
            time.sleep(1)
            bar.advance(task)


@app.command("list")
def list_scenarios() -> None:
    """List available scenarios."""
    for path in scenario_files():
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        console.print(f"[bold]{data['id']}[/]  {escape(data['description'])}")


@app.command()
def apply(scenario_id: str) -> None:
    """Inject a scenario's fault and return immediately (no reset, no waiting)."""
    for step in apply_setup(load_scenario(scenario_id)):
        say(f"✓ {step.description}")


@app.command()
def deploy(service: str, patch: str) -> None:
    """Deploy a patch from chaos/patches/ to one service (commit + rebuild + deploy event)."""
    say(f"✓ deployed {json.dumps(deploy_sim.deploy(service, patch))}")


@app.command()
def scenario(scenario_id: str,
             observe: int = typer.Option(None, help="Seconds of fault before the alert (default: observe_s)."),
             warmup: int = typer.Option(None, help="Seconds of healthy traffic before the fault (default: warmup_s)."),
             noise: bool = typer.Option(True, help="Deploy harmless changes before the warm-up."),
             out: str = typer.Option(None, help="Alert output path (default: alerts/<sNN>.json).")) -> None:
    """Run a scenario end to end and write the alert JSON for the agents."""
    scn = load_scenario(scenario_id)
    console.rule(f"[bold]{scn.id}[/]: {escape(scn.description)}")
    run = runner.run_until_alert(scn, say, wait, warmup=warmup, observe=observe, noise=noise)

    path = faults.ROOT / (out or f"alerts/{scn.id.split('_')[0]}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(run.alert, indent=2) + "\n", encoding="utf-8")
    say(f"✓ alert written to {path.relative_to(faults.ROOT)}")
    if not run.symptoms_ok:
        console.print("[yellow]! the symptom did not fully develop - the investigation may be inconclusive[/]")
    console.print(f"  next: [bold]make investigate ALERT={path.relative_to(faults.ROOT)}[/]   (afterwards: make reset)")


@app.command()
def reset() -> None:
    """Back to the healthy baseline: faults off, target-repo at `baseline`, everything running."""
    try:
        runner.reset_all(say)
    except RuntimeError as exc:
        say(f"✗ {exc}")
        raise typer.Exit(1)
