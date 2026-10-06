"""dependency_graph tool (Diagnostician): the static service topology from topology.yaml."""

from __future__ import annotations

from functools import cache
from pathlib import Path

import yaml

from responder.tools.base import NoArgs, Tool, ToolContext

TOPOLOGY_PATH = Path(__file__).resolve().parent.parent / "topology.yaml"


@cache
def load_topology() -> dict[str, dict]:
    """component name -> {kind, description, depends_on}"""
    return yaml.safe_load(TOPOLOGY_PATH.read_text(encoding="utf-8"))["components"]


def downstream_of(service: str) -> list[str]:
    """All components `service` depends on, directly or transitively (breadth-first)."""
    topo, seen, queue = load_topology(), [], [service]
    while queue:
        for dep in topo.get(queue.pop(0), {}).get("depends_on", []):
            if dep not in seen:
                seen.append(dep)
                queue.append(dep)
    return seen


def dependency_graph(args: NoArgs, ctx: ToolContext) -> dict:
    topo = load_topology()
    return {
        "components": {name: {"kind": c["kind"], "description": c["description"]} for name, c in topo.items()},
        "calls": [f"{name} -> {dep}" for name, c in topo.items() for dep in c["depends_on"]],
    }


DEPENDENCY_GRAPH = Tool("dependency_graph",
                        "Static dependency graph: components, their kind, and who calls whom (caller -> dependency).",
                        NoArgs, dependency_graph)
