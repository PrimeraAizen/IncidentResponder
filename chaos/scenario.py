"""Scenario files (scenarios/sNN_*.yaml): loading, fault injection, alert building.

A scenario = fault setup steps + the alert the system will receive (the
SYMPTOM) + the expected root cause (ground truth, used only for scoring).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from chaos import faults

SCENARIOS_DIR = faults.ROOT / "scenarios"
# Real alerts fire some time after the fault starts (rule `for:` clause).
ALERT_DELAY = timedelta(seconds=30)


class Scenario(BaseModel):
    id: str
    description: str
    setup: list[dict[str, Any]]
    load: dict[str, Any] = Field(default_factory=dict)
    warmup_s: int = 60
    observe_s: int = 90
    alert: dict[str, Any]
    expected: dict[str, Any]


def scenario_files() -> list[Path]:
    return sorted(SCENARIOS_DIR.glob("s[0-9][0-9]_*.yaml"))


def load_scenario(scenario_id: str) -> Scenario:
    """Accepts 's01' or the full id 's01_payment_latency'."""
    matches = [p for p in scenario_files() if p.stem == scenario_id or p.stem.startswith(f"{scenario_id}_")]
    if len(matches) != 1:
        raise ValueError(f"scenario {scenario_id!r} not found in {SCENARIOS_DIR}")
    return Scenario.model_validate(yaml.safe_load(matches[0].read_text(encoding="utf-8")))


def apply_setup(scn: Scenario) -> None:
    """Execute the scenario's fault steps."""
    for step in scn.setup:
        if "toxic" in step:
            spec = dict(step["toxic"])
            faults.add_toxic(spec.pop("proxy"), spec.pop("type"), **spec)
        else:
            raise NotImplementedError(f"step type {list(step)} is not implemented yet (Phase 2)")


def build_alert(scn: Scenario, injected_at: datetime) -> dict[str, Any]:
    """The alert JSON given to the agents. It contains ONLY the symptom - no scenario id, no ground truth."""
    alert = {k: scn.alert[k] for k in ("alert_name", "service", "severity", "summary")}
    alert["started_at"] = (injected_at + ALERT_DELAY).isoformat()
    alert["labels"] = {"alertname": scn.alert["alert_name"], "service": scn.alert["service"]}
    return alert
