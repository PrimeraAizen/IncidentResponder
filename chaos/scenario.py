"""Scenario files (scenarios/sNN_*.yaml): loading, fault injection, symptom check, alert.

A scenario = fault setup steps + the alert the system will receive (the
SYMPTOM) + the expected root cause (ground truth, used only for scoring) +
a symptom check that proves the fault really produced the symptom.

Setup step types:
    - toxic:  {proxy: gateway_payment, type: latency, latency_ms: 2000}
    - docker: {stop: postgres}
    - chaos:  {service: notification, leak_memory: true}
    - deploy: {service: order, patch: s04_order_promotions.patch}
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import BaseModel, Field

from chaos import deploy_sim, faults

SCENARIOS_DIR = faults.ROOT / "scenarios"
STEP_TYPES = {"toxic", "docker", "chaos", "deploy"}
# Real alerts fire some time after the fault starts (rule `for:` clause).
ALERT_DELAY = timedelta(seconds=30)


class SymptomCheck(BaseModel):
    """A PromQL expression whose largest value must be above (or below) a threshold."""
    promql: str
    above: float | None = None
    below: float | None = None
    description: str = ""


class Scenario(BaseModel):
    id: str
    description: str
    setup: list[dict[str, Any]]
    load: dict[str, Any] = Field(default_factory=dict)   # {rps: total req/s, mix: extra traffic mix, duration_s: after injection}
    warmup_s: int = 120
    observe_s: int = 90
    noise_deploys: list[str] | None = None               # default: chosen by deploy_sim.noise_services
    symptom: list[SymptomCheck] = Field(default_factory=list)
    alert: dict[str, Any]
    expected: dict[str, Any]

    def ground_truth_services(self) -> set[str]:
        return {self.expected["root_cause_service"], *self.expected.get("accepted_services", [])}

    def noise_services(self) -> list[str]:
        if self.noise_deploys is not None:
            return self.noise_deploys
        return deploy_sim.noise_services(avoid=self.ground_truth_services())


def scenario_files() -> list[Path]:
    return sorted(SCENARIOS_DIR.glob("s[0-9][0-9]_*.yaml"))


def load_scenario(scenario_id: str) -> Scenario:
    """Accepts 's01' or the full id 's01_payment_latency'."""
    matches = [p for p in scenario_files() if p.stem == scenario_id or p.stem.startswith(f"{scenario_id}_")]
    if len(matches) != 1:
        raise ValueError(f"scenario {scenario_id!r} not found in {SCENARIOS_DIR}")
    return Scenario.model_validate(yaml.safe_load(matches[0].read_text(encoding="utf-8")))


@dataclass
class StepResult:
    description: str
    deploy_event: dict | None = None  # set for deploy steps: the commit the scenario expects to be blamed


def apply_setup(scn: Scenario) -> list[StepResult]:
    """Execute the scenario's fault steps in order."""
    done = []
    for step in scn.setup:
        if len(step) != 1:
            raise ValueError(f"each setup step must have exactly one type, got {list(step)}")
        kind, spec = next(iter(step.items()))
        spec = dict(spec)
        if kind == "toxic":
            faults.add_toxic(spec.pop("proxy"), spec.pop("type"), **spec)
            done.append(StepResult(f"toxic {step['toxic']}"))
        elif kind == "docker":
            faults.stop_service(spec["stop"])
            done.append(StepResult(f"docker stop {spec['stop']}"))
        elif kind == "chaos":
            service = spec.pop("service")
            faults.set_chaos_flags(service, **spec)
            done.append(StepResult(f"/admin/chaos on {service}: {spec}"))
        elif kind == "deploy":
            event = deploy_sim.deploy(spec["service"], spec["patch"])
            done.append(StepResult(f"deploy {event['service']} {event['version']} ({event['commit']}): "
                                   f"{event['message']}", deploy_event=event))
        else:
            raise ValueError(f"unknown setup step {kind!r} (known: {sorted(STEP_TYPES)})")
    return done


def check_symptoms(scn: Scenario) -> list[tuple[SymptomCheck, float, bool]]:
    """Evaluate every symptom check against Prometheus now. Missing series count as 0."""
    prom = os.getenv("PROM_URL", "http://localhost:9090")
    results = []
    for check in scn.symptom:
        data = httpx.get(f"{prom}/api/v1/query", params={"query": check.promql}, timeout=10).json()["data"]
        values = [float(r["value"][1]) for r in data["result"] if r["value"][1] not in ("NaN", "+Inf", "-Inf")]
        value = max(values, default=0.0)
        ok = (check.above is None or value > check.above) and (check.below is None or value < check.below)
        results.append((check, value, ok))
    return results


def build_alert(scn: Scenario, fault_started_at: datetime) -> dict[str, Any]:
    """The alert JSON given to the agents. It contains ONLY the symptom - no scenario id, no ground truth."""
    alert = {k: scn.alert[k] for k in ("alert_name", "service", "severity", "summary")}
    alert["started_at"] = (fault_started_at + ALERT_DELAY).isoformat()
    alert["labels"] = {"alertname": scn.alert["alert_name"], "service": scn.alert["service"]}
    return alert
