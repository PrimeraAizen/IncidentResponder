"""Deterministic investigation plan: the Orchestrator's fallback (and its default values).

Builds a valid InvestigationPlan without an LLM: a window before the alert,
the focus services (the alerting service plus everything it depends on) and
one Task per specialist. Used when the Orchestrator's LLM call fails, and to
fill in anything the Orchestrator leaves out.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from responder.schemas import Alert, InvestigationPlan, Task
from responder.tools.topology import downstream_of

DEFAULT_LOOKBACK_MIN = 10

# The three specialists and the default objective of each (formatted with the alert).
SPECIALIST_OBJECTIVES = {
    "log_analyst": "Find warning/error log patterns, when they started and which services emit them, "
                   "around the {alert_name} alert on {service}.",
    "metrics_analyst": "Find anomalies in latency, error rates, calls between services, DB pool, cache, events "
                       "and resource metrics for {focus}, compared with the baseline before the {alert_name} alert.",
    "change_investigator": "Find deploys, commits, config changes and container restarts or stops near the "
                           "{alert_name} alert, especially in {focus}.",
}


def build_plan(alert: Alert, incident_id: str, now: datetime, lookback_min: int = DEFAULT_LOOKBACK_MIN,
               focus: list[str] | None = None, objectives: dict[str, str] | None = None) -> InvestigationPlan:
    window_start = alert.started_at - timedelta(minutes=lookback_min)
    window_end = max(now, alert.started_at + timedelta(minutes=1))
    focus = focus or [alert.service] + downstream_of(alert.service)
    objectives = objectives or {}
    fields = {"alert_name": alert.alert_name, "service": alert.service, "focus": ", ".join(focus)}

    tasks = [Task(task_id=f"{incident_id}-{agent.split('_')[0]}", agent=agent,
                  objective=objectives.get(agent) or template.format(**fields),
                  window_start=window_start, window_end=window_end, focus_services=focus)
             for agent, template in SPECIALIST_OBJECTIVES.items()]
    return InvestigationPlan(incident_id=incident_id, alert=alert, window_start=window_start,
                             window_end=window_end, focus_services=focus, tasks=tasks)
