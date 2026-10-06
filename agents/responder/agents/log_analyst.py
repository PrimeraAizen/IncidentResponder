"""Log Analyst: finds error patterns, their onset time and affected services in logs.

Input: Task. Output: EvidenceBundle (source="logs", ids logs-1, logs-2, ...).
Tools (3): loki_query, loki_label_values, error_pattern_summary.
Done when: ≥1 evidence item or an explicit "no anomalies", within budget.
"""

from __future__ import annotations

from responder.agents.common import run_specialist
from responder.runtime import RunContext
from responder.schemas import AgentFailure, Alert, EvidenceBundle, Task
from responder.tools.loki import ERROR_PATTERN_SUMMARY, LOKI_LABEL_VALUES, LOKI_QUERY

NAME = "log_analyst"
TOOLS = [ERROR_PATTERN_SUMMARY, LOKI_QUERY, LOKI_LABEL_VALUES]


def run(task: Task, alert: Alert, ctx: RunContext) -> tuple[EvidenceBundle, AgentFailure | None]:
    return run_specialist(name=NAME, source="logs", id_prefix="logs", tools=TOOLS,
                          task=task, alert=alert, run=ctx)
