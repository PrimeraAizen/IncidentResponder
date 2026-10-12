"""Anti-leakage and tool-count rules (SPEC 2.5 and 6.4)."""

import ast
from pathlib import Path

from responder.agents import (change_investigator, diagnostician, log_analyst, metrics_analyst, orchestrator,
                              reporter, verifier)
from responder.tools import ALL_TOOLS

# Every tool the spec allows (section 6.4). Nothing else may ever be registered.
ALLOWED_TOOLS = {
    "loki_query", "loki_label_values", "error_pattern_summary",
    "prom_query", "prom_query_range", "zscore_anomaly", "targets_health",
    "deploy_events", "git_log", "git_diff", "docker_inspect",
    "dependency_graph", "health_check", "write_report",
}
AGENTS = [orchestrator, log_analyst, metrics_analyst, change_investigator, diagnostician, verifier, reporter]
TOOLS_DIR = Path(__file__).resolve().parents[1] / "responder" / "tools"


def test_registry_contains_exactly_the_allowed_tools():
    assert set(ALL_TOOLS) == ALLOWED_TOOLS


def test_seven_agents_with_distinct_roles():
    assert len({a.NAME for a in AGENTS}) == 7
    assert orchestrator.TOOLS == []  # the orchestrator only plans


def test_tool_sets_match_the_spec():
    expected = {
        "log_analyst": {"loki_query", "loki_label_values", "error_pattern_summary"},
        "metrics_analyst": {"prom_query", "prom_query_range", "zscore_anomaly", "targets_health"},
        "change_investigator": {"deploy_events", "git_log", "git_diff", "docker_inspect"},
        "diagnostician": {"dependency_graph"},
        "verifier": {"prom_query", "loki_query", "health_check"},
        "reporter": {"write_report"},
        "orchestrator": set(),
    }
    assert {a.NAME: {t.name for t in a.TOOLS} for a in AGENTS} == expected


def test_every_agent_tool_is_registered():
    for agent in AGENTS:
        for tool in agent.TOOLS:
            assert ALL_TOOLS.get(tool.name) is tool, f"{agent.NAME} uses unregistered tool {tool.name}"


def test_no_agent_has_more_than_five_tools():
    for agent in AGENTS:
        assert len(agent.TOOLS) <= 5, agent.NAME


def test_tool_code_never_touches_the_harness():
    """No string literal or name in tool CODE may refer to the harness (docstrings and comments may explain the rule)."""
    forbidden = ["scenarios", "chaos", "toxiproxy", "8474", "loadgen"]
    for path in TOOLS_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                      if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
                      and node.body and isinstance(node.body[0], ast.Expr)
                      and isinstance(node.body[0].value, ast.Constant)}
        for node in ast.walk(tree):
            text = None
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                text = node.value
            elif isinstance(node, ast.Name):
                text = node.id
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                text = " ".join(a.name for a in node.names) + " " + (getattr(node, "module", "") or "")
            if text:
                for word in forbidden:
                    assert word not in text.lower(), f"{path.name} refers to {word!r}: {text[:80]!r}"
