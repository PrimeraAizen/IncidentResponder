"""Chaos CLI: inject scenarios, reset to baseline.

    python -m chaos list
    python -m chaos scenario s01     # inject, wait observe_s, write alerts/s01.json
    python -m chaos apply s01        # inject only
    python -m chaos reset            # back to the healthy baseline
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import httpx
import typer
import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn

from chaos import faults
from chaos.scenario import apply_setup, build_alert, load_scenario, scenario_files

app = typer.Typer(add_completion=False, help="Fault-injection harness (never used by the agents).")
console = Console()
load_dotenv(faults.ROOT / ".env", override=False)


@app.command("list")
def list_scenarios() -> None:
    """List available scenarios."""
    for path in scenario_files():
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        console.print(f"[bold]{data['id']}[/]  {data['description']}")


@app.command()
def apply(scenario_id: str) -> None:
    """Inject a scenario's fault and return immediately."""
    scn = load_scenario(scenario_id)
    apply_setup(scn)
    console.print(f"[green]✓[/] injected {scn.id}: {scn.description}")


@app.command()
def scenario(scenario_id: str,
             observe: int = typer.Option(None, help="Seconds to wait after injection (default: scenario observe_s)."),
             out: str = typer.Option(None, help="Alert output path (default: alerts/<sNN>.json).")) -> None:
    """Inject a scenario, let the symptom develop, then write the alert JSON for the agents."""
    scn = load_scenario(scenario_id)
    short_id = scn.id.split("_")[0]
    if not faults.wait_healthy(timeout_s=10):
        console.print("[red]✗ gateway is not healthy - run `make up` (or `make reset`) first.[/]")
        raise typer.Exit(1)
    _ensure_warmup(scn.warmup_s)

    injected_at = datetime.now(timezone.utc)
    apply_setup(scn)
    console.print(f"[green]✓[/] {injected_at:%H:%M:%S} injected [bold]{scn.id}[/]: {scn.description}")

    _wait("fault active, waiting for symptoms", observe or scn.observe_s)
    alert = build_alert(scn, injected_at)
    path = faults.ROOT / (out or f"alerts/{short_id}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(alert, indent=2) + "\n", encoding="utf-8")
    console.print(f"[green]✓[/] alert written to {path.relative_to(faults.ROOT)}")
    console.print(f"  next: [bold]make investigate ALERT={path.relative_to(faults.ROOT)}[/]   "
                  f"(afterwards: make reset)")


@app.command()
def reset() -> None:
    """Return to the healthy baseline: clear toxics, reset target-repo, start stopped containers."""
    faults.reset_toxiproxy()
    console.print("[green]✓[/] toxiproxy: all toxics removed")
    if faults.reset_target_repo():
        console.print("  target-repo changed since baseline: rebuilding services…")
        faults.compose("up", "-d", "--build", *faults.SHOP_SERVICES)
        console.print("[green]✓[/] target-repo reset to baseline and services rebuilt")
    else:
        console.print("[green]✓[/] target-repo already at baseline")
    faults.compose("up", "-d")  # starts anything that was stopped
    if faults.wait_healthy():
        console.print("[green]✓[/] gateway healthy")
    else:
        console.print("[red]✗ gateway did not become healthy within 120 s[/]")
        raise typer.Exit(1)


def _ensure_warmup(warmup_s: int) -> None:
    """Agents need a healthy baseline before the fault. Wait if the gateway started recently."""
    prom = os.getenv("PROM_URL", "http://localhost:9090")
    try:
        resp = httpx.get(f"{prom}/api/v1/query", timeout=5,
                         params={"query": 'time() - process_start_time_seconds{service="gateway"}'})
        uptime = float(resp.json()["data"]["result"][0]["value"][1])
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        uptime = 0.0
    if uptime < warmup_s:
        _wait("warming up (healthy baseline traffic)", int(warmup_s - uptime))


def _wait(label: str, seconds: int) -> None:
    with Progress(TextColumn("  {task.description}"), BarColumn(), TimeRemainingColumn(), console=console) as bar:
        task = bar.add_task(label, total=seconds)
        for _ in range(seconds):
            time.sleep(1)
            bar.advance(task)
