"""Deterministic investigation plan (Phase 1 stand-in for the Orchestrator agent).

Builds the same InvestigationPlan the Orchestrator LLM agent will produce in
Phase 3: a time window around the alert, the focus services (the alerting
service plus everything it depends on), and one Task per specialist.
Once the Orchestrator exists, this builder stays as its fallback when the
Orchestrator's LLM call fails.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from responder.schemas import Alert, InvestigationPlan, Task
from responder.tools.topology import downstream_of

LOOKBACK = timedelta(minutes=15)  # baseline before the alert


def build_plan(alert: Alert, incident_id: str, now: datetime) -> InvestigationPlan:
    window_start = alert.started_at - LOOKBACK
    window_end = max(now, alert.started_at + timedelta(minutes=1))
    focus = [alert.service] + downstream_of(alert.service)

    def task(agent: str, suffix: str, objective: str) -> Task:
        return Task(task_id=f"{incident_id}-{suffix}", agent=agent, objective=objective,
                    window_start=window_start, window_end=window_end, focus_services=focus)

    tasks = [
        task("log_analyst", "logs",
             f"Find warning/error log patterns, when they started and which services emit them, "
             f"around the {alert.alert_name} alert on {alert.service}."),
        task("metrics_analyst", "metrics",
             f"Find anomalies in latency, error rates, calls between services, DB pool and resource metrics "
             f"for {', '.join(focus)}, compared with the baseline before the {alert.alert_name} alert."),
    ]
    return InvestigationPlan(incident_id=incident_id, alert=alert, window_start=window_start,
                             window_end=window_end, focus_services=focus, tasks=tasks)
