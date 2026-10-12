"""Chaos CLI: run scenarios, simulate deploys, reset to baseline.

    python -m chaos list
    python -m chaos scenario s01     # full run: reset, noise deploys, warm-up, fault, symptom check, alert
    python -m chaos apply s01        # inject only
    python -m chaos deploy order s04_order_promotions.patch
    python -m chaos reset            # back to the healthy baseline
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import typer
import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn

from chaos import deploy_sim, faults
from chaos.scenario import Scenario, apply_setup, build_alert, check_symptoms, load_scenario, scenario_files

app = typer.Typer(add_completion=False, help="Fault-injection harness (never used by the agents).")
console = Console()
load_dotenv(faults.ROOT / ".env", override=False)


@app.command("list")
def list_scenarios() -> None:
    """List available scenarios."""
    for path in scenario_files():
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        console.print(f"[bold]{data['id']}[/]  {escape(data['description'])}")


@app.command()
def apply(scenario_id: str) -> None:
    """Inject a scenario's fault and return immediately (no reset, no waiting)."""
    for line in apply_setup(load_scenario(scenario_id)):
        console.print(f"[green]✓[/] {escape(line)}")


@app.command()
def deploy(service: str, patch: str) -> None:
    """Deploy a patch from chaos/patches/ to one service (commit + rebuild + deploy event)."""
    event = deploy_sim.deploy(service, patch)
    console.print(f"[green]✓[/] deployed {escape(json.dumps(event))}")


@app.command()
def scenario(scenario_id: str,
             observe: int = typer.Option(None, help="Seconds of fault before the alert (default: observe_s)."),
             warmup: int = typer.Option(None, help="Seconds of healthy traffic before the fault (default: warmup_s)."),
             noise: bool = typer.Option(True, help="Deploy harmless changes before the warm-up."),
             out: str = typer.Option(None, help="Alert output path (default: alerts/<sNN>.json).")) -> None:
    """Run a scenario end to end and write the alert JSON for the agents."""
    scn = load_scenario(scenario_id)
    console.rule(f"[bold]{scn.id}[/]: {escape(scn.description)}")
    reset()
    if noise:
        for event in deploy_sim.deploy_noise(scn.noise_services()):
            console.print(f"[green]✓[/] noise deploy {event['service']} {event['version']} "
                          f"({event['commit']}): {escape(event['message'])}")
    _start_scenario_load(scn)
    _wait("warm-up: healthy traffic before the fault", scn.warmup_s if warmup is None else warmup)

    for line in apply_setup(scn):
        console.print(f"[green]✓[/] injected: {escape(line)}")
    fault_started_at = datetime.now(timezone.utc)
    _wait("fault active, waiting for symptoms", scn.observe_s if observe is None else observe)

    all_ok = True
    for check, value, ok in check_symptoms(scn):
        all_ok &= ok
        bound = f"> {check.above}" if check.above is not None else f"< {check.below}"
        mark = "[green]✓[/]" if ok else "[red]✗[/]"
        console.print(f"{mark} symptom: {escape(check.description or check.promql)} = {value:.4g} (want {bound})")

    alert = build_alert(scn, fault_started_at)
    path = faults.ROOT / (out or f"alerts/{scn.id.split('_')[0]}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(alert, indent=2) + "\n", encoding="utf-8")
    console.print(f"[green]✓[/] alert written to {path.relative_to(faults.ROOT)}")
    if not all_ok:
        console.print("[yellow]! the symptom did not fully develop - the investigation may be inconclusive[/]")
    console.print(f"  next: [bold]make investigate ALERT={path.relative_to(faults.ROOT)}[/]   (afterwards: make reset)")


@app.command()
def reset() -> None:
    """Back to the healthy baseline: faults off, target-repo at `baseline`, everything running."""
    faults.stop_extra_load()
    faults.reset_toxiproxy()
    console.print("[green]✓[/] toxiproxy: scenario toxics removed (baseline DB latency kept)")
    faults.start_stopped()
    faults.clear_chaos_flags()
    console.print("[green]✓[/] all containers running, in-process faults off")
    changed = faults.reset_target_repo()
    if changed:
        console.print(f"  target-repo differs from baseline in {', '.join(changed)}: rebuilding…")
        faults.rebuild(changed)
    console.print("[green]✓[/] target-repo at baseline")
    faults.clear_deploy_events()
    console.print("[green]✓[/] deploy log cleared")
    if faults.wait_healthy():
        console.print("[green]✓[/] gateway healthy")
    else:
        console.print("[red]✗ gateway did not become healthy within 120 s[/]")
        raise typer.Exit(1)


def _start_scenario_load(scn: Scenario) -> None:
    """`load.rps` is the total rate during the scenario; the always-on loadgen provides LOADGEN_RPS of it.
    `load.mix` (optional) is the request mix of the extra traffic, e.g. "create=70,get=30"."""
    extra = float(scn.load.get("rps", 0)) - float(os.getenv("LOADGEN_RPS", 10))
    if extra > 0:
        duration = scn.warmup_s + int(scn.load.get("duration_s", scn.observe_s + 300))
        mix = scn.load.get("mix")
        faults.start_extra_load(extra, duration, mix)
        console.print(f"[green]✓[/] extra load: +{extra:g} rps for {duration} s" + (f" (mix {mix})" if mix else ""))


def _wait(label: str, seconds: int) -> None:
    with Progress(TextColumn("  {task.description}"), BarColumn(), TimeRemainingColumn(), console=console) as bar:
        task = bar.add_task(label, total=seconds)
        for _ in range(seconds):
            time.sleep(1)
            bar.advance(task)
