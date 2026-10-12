"""Tool registry: the ONLY tools any agent can use (SPEC 6.4).

Anti-leakage rule: no tool may read scenarios/, chaos/, /admin/chaos, the
Toxiproxy API or any harness state - the agents must find the root cause from
observability data, git, the deploy log and Docker state, like a real on-call
engineer. tests/test_tools_registry.py asserts that this registry contains
exactly the allowed tool names.
"""

from responder.tools.base import Tool
from responder.tools.deploys import DEPLOY_EVENTS
from responder.tools.docker import DOCKER_INSPECT
from responder.tools.git import GIT_DIFF, GIT_LOG
from responder.tools.health import HEALTH_CHECK
from responder.tools.loki import ERROR_PATTERN_SUMMARY, LOKI_LABEL_VALUES, LOKI_QUERY
from responder.tools.prometheus import PROM_QUERY, PROM_QUERY_RANGE, TARGETS_HEALTH, ZSCORE_ANOMALY
from responder.tools.report import WRITE_REPORT
from responder.tools.topology import DEPENDENCY_GRAPH

ALL_TOOLS: dict[str, Tool] = {t.name: t for t in [
    LOKI_QUERY, LOKI_LABEL_VALUES, ERROR_PATTERN_SUMMARY,                 # logs
    PROM_QUERY, PROM_QUERY_RANGE, ZSCORE_ANOMALY, TARGETS_HEALTH,         # metrics
    DEPLOY_EVENTS, GIT_LOG, GIT_DIFF, DOCKER_INSPECT,                     # changes
    DEPENDENCY_GRAPH,                                                     # topology
    HEALTH_CHECK,                                                         # verification
    WRITE_REPORT,                                                         # output
]}
