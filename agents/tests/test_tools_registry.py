"""Anti-leakage and tool-count rules (SPEC 2.5 and 6.4)."""

from pathlib import Path

from responder.agents import diagnostician, log_analyst, metrics_analyst
from responder.tools import ALL_TOOLS

# Every tool the spec allows (section 6.4). Nothing else may ever be registered.
ALLOWED_TOOLS = {
    "loki_query", "loki_label_values", "error_pattern_summary",
    "prom_query", "prom_query_range", "zscore_anomaly", "targets_health",
    "deploy_events", "git_log", "git_diff", "docker_inspect",
    "dependency_graph", "health_check", "write_report",
}
AGENTS = [log_analyst, metrics_analyst, diagnostician]
TOOLS_DIR = Path(__file__).resolve().parents[1] / "responder" / "tools"


def test_registry_contains_only_allowed_tools():
    assert set(ALL_TOOLS) <= ALLOWED_TOOLS


def test_every_agent_tool_is_registered():
    for agent in AGENTS:
        for tool in agent.TOOLS:
            assert ALL_TOOLS.get(tool.name) is tool, f"{agent.NAME} uses unregistered tool {tool.name}"


def test_no_agent_has_more_than_five_tools():
    for agent in AGENTS:
        assert len(agent.TOOLS) <= 5, agent.NAME


def test_tool_code_never_touches_the_harness():
    forbidden = ["scenarios", "chaos", "toxiproxy", "8474", "admin/chaos"]
    for path in TOOLS_DIR.glob("*.py"):
        code = path.read_text(encoding="utf-8").lower()
        # The registry docstring explains the rule itself; check only code lines elsewhere.
        if path.name == "__init__.py":
            code = code.split('"""', 2)[-1]
        for word in forbidden:
            assert word not in code, f"{path.name} mentions {word!r}"
