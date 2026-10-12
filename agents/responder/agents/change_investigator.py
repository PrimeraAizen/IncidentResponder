"""Change Investigator: finds deploys, commits, config changes and container restarts near the incident.

Input: Task. Output: EvidenceBundle (source="changes", ids changes-1, ...);
a deploy or commit is reported with its sha in raw_ref (the suspect commit, if any).
Tools (4): deploy_events, git_log, git_diff, docker_inspect.
Done when: ≥1 evidence item or an explicit "no changes", within budget.
"""

from __future__ import annotations

from responder.agents.common import run_specialist
from responder.runtime import RunContext
from responder.schemas import AgentFailure, Alert, EvidenceBundle, Task
from responder.tools.deploys import DEPLOY_EVENTS
from responder.tools.docker import DOCKER_INSPECT
from responder.tools.git import GIT_DIFF, GIT_LOG

NAME = "change_investigator"
TOOLS = [DEPLOY_EVENTS, DOCKER_INSPECT, GIT_DIFF, GIT_LOG]


def run(task: Task, alert: Alert, ctx: RunContext) -> tuple[EvidenceBundle, AgentFailure | None]:
    return run_specialist(name=NAME, source="changes", id_prefix="changes", tools=TOOLS,
                          task=task, alert=alert, run=ctx)
