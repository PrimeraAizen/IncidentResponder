"""Tool registry: the ONLY tools any agent can use.

Anti-leakage rule (SPEC 6.4): no tool may read scenarios/, chaos/,
/admin/chaos, the Toxiproxy API or any harness state - the agents must find
the root cause from observability data, git, the deploy log and Docker state,
like a real on-call engineer. tests/test_tools_registry.py asserts that this
registry only contains allowed tool names.

Phase 1 implements the log, metrics and topology tools; the change, health
and report tools arrive with their agents in Phase 3.
"""

from responder.tools.base import Tool
from responder.tools.loki import ERROR_PATTERN_SUMMARY, LOKI_LABEL_VALUES, LOKI_QUERY
from responder.tools.prometheus import PROM_QUERY, PROM_QUERY_RANGE, TARGETS_HEALTH, ZSCORE_ANOMALY
from responder.tools.topology import DEPENDENCY_GRAPH

ALL_TOOLS: dict[str, Tool] = {t.name: t for t in [
    LOKI_QUERY, LOKI_LABEL_VALUES, ERROR_PATTERN_SUMMARY,
    PROM_QUERY, PROM_QUERY_RANGE, ZSCORE_ANOMALY, TARGETS_HEALTH,
    DEPENDENCY_GRAPH,
]}
