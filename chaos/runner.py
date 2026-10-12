"""One scenario run up to the alert, shared by `python -m chaos scenario` and eval/run_scenario.py.

    reset → noise deploys → scenario load → warm-up → fault → observe → symptom check → alert

`say` prints a progress line and `wait` waits with a progress bar, so the caller
decides how output looks.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from chaos import deploy_sim, faults
from chaos.scenario import Scenario, SymptomCheck, apply_setup, build_alert, check_symptoms

Say = Callable[[str], None]
Wait = Callable[[str, int], None]


@dataclass
class ScenarioRun:
    scenario: Scenario
    alert: dict
    fault_started_at: datetime
    noise_deploys: list[dict] = field(default_factory=list)
    scenario_deploys: list[dict] = field(default_factory=list)   # deploy events created by the fault itself
    symptoms: list[tuple[SymptomCheck, float, bool]] = field(default_factory=list)

    @property
    def symptoms_ok(self) -> bool:
        return all(ok for _, _, ok in self.symptoms)


def reset_all(say: Say) -> None:
    """Back to the healthy baseline: faults off, target-repo at `baseline`, everything running."""
    faults.stop_extra_load()
    faults.reset_toxiproxy()
    say("✓ toxiproxy: scenario toxics removed (baseline DB latency kept)")
    faults.start_stopped()
    faults.clear_chaos_flags()
    say("✓ all containers running, in-process faults off")
    changed = faults.reset_target_repo()
    if changed:
        say(f"  target-repo differs from baseline in {', '.join(changed)}: rebuilding…")
        faults.rebuild(changed)
    say("✓ target-repo at baseline")
    faults.clear_deploy_events()
    say("✓ deploy log cleared")
    if not faults.wait_healthy():
        raise RuntimeError("gateway did not become healthy within 120 s after reset")
    say("✓ gateway healthy")


def run_until_alert(scn: Scenario, say: Say, wait: Wait, warmup: int | None = None, observe: int | None = None,
                    noise: bool = True) -> ScenarioRun:
    reset_all(say)
    noise_events = deploy_sim.deploy_noise(scn.noise_services()) if noise else []
    for e in noise_events:
        say(f"✓ noise deploy {e['service']} {e['version']} ({e['commit']}): {e['message']}")
    _start_scenario_load(scn, say)
    wait("warm-up: healthy traffic before the fault", scn.warmup_s if warmup is None else warmup)

    steps = apply_setup(scn)
    for step in steps:
        say(f"✓ injected: {step.description}")
    fault_started_at = datetime.now(timezone.utc)
    wait("fault active, waiting for symptoms", scn.observe_s if observe is None else observe)

    symptoms = check_symptoms(scn)
    for check, value, ok in symptoms:
        bound = f"> {check.above}" if check.above is not None else f"< {check.below}"
        say(f"{'✓' if ok else '✗'} symptom: {check.description or check.promql} = {value:.4g} (want {bound})")
    return ScenarioRun(scenario=scn, alert=build_alert(scn, fault_started_at), fault_started_at=fault_started_at,
                       noise_deploys=noise_events,
                       scenario_deploys=[s.deploy_event for s in steps if s.deploy_event],
                       symptoms=symptoms)


def _start_scenario_load(scn: Scenario, say: Say) -> None:
    """`load.rps` is the total rate during the scenario; the always-on loadgen provides LOADGEN_RPS of it.
    `load.mix` (optional) is the request mix of the extra traffic, e.g. "create=70,get=30"."""
    extra = float(scn.load.get("rps", 0)) - float(os.getenv("LOADGEN_RPS", 10))
    if extra > 0:
        duration = scn.warmup_s + int(scn.load.get("duration_s", scn.observe_s + 300))
        mix = scn.load.get("mix")
        faults.start_extra_load(extra, duration, mix)
        say(f"✓ extra load: +{extra:g} rps for {duration} s" + (f" (mix {mix})" if mix else ""))
