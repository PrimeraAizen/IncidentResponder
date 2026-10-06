"""Metrics Analyst: finds anomalies in latency, error rate, resources, pool/cache/event metrics.

Input: Task. Output: EvidenceBundle (source="metrics", ids metrics-1, metrics-2, ...).
Tools (4): prom_query, prom_query_range, zscore_anomaly, targets_health.
Done when: ≥1 evidence item or an explicit "no anomalies", within budget.
"""

from __future__ import annotations

from responder.agents.common import run_specialist
from responder.runtime import RunContext
from responder.schemas import AgentFailure, Alert, EvidenceBundle, Task
from responder.tools.prometheus import PROM_QUERY, PROM_QUERY_RANGE, TARGETS_HEALTH, ZSCORE_ANOMALY

NAME = "metrics_analyst"
TOOLS = [ZSCORE_ANOMALY, PROM_QUERY_RANGE, PROM_QUERY, TARGETS_HEALTH]


def run(task: Task, alert: Alert, ctx: RunContext) -> tuple[EvidenceBundle, AgentFailure | None]:
    return run_specialist(name=NAME, source="metrics", id_prefix="metrics", tools=TOOLS,
                          task=task, alert=alert, run=ctx)
