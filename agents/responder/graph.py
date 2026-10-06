"""LangGraph wiring of the agents. Routing is plain code (graph edges), never an LLM call.

Phase 1 prototype graph:

    START → plan ─┬─► log_analyst ─────┬─► diagnostician → END
                  └─► metrics_analyst ─┘

The two specialists run in parallel; the diagnostician starts when both are done.
Phase 3 replaces `plan` with the Orchestrator agent and adds the Change
Investigator branch, the Verifier loop and the Reporter.
"""

from __future__ import annotations

import json
import operator
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Callable, TypedDict

import httpx
from langgraph.graph import END, START, StateGraph
from rich.console import Console

from responder.agents import diagnostician, log_analyst, metrics_analyst
from responder.agents.planner import build_plan
from responder.calllog import CallLogger
from responder.config import Settings
from responder.llm import LLM, make_llm
from responder.runtime import RunContext
from responder.schemas import AgentFailure, Alert, EvidenceBundle, HypothesisSet, InvestigationPlan


class GraphState(TypedDict, total=False):
    """Explicit task state passed between nodes."""
    alert: Alert
    plan: InvestigationPlan
    bundles: Annotated[list[EvidenceBundle], operator.add]   # parallel specialists append here
    failures: Annotated[list[AgentFailure], operator.add]
    hypotheses: HypothesisSet | None


def build_graph(ctx: RunContext, incident_id: str):
    def plan_node(state: GraphState) -> dict:
        plan = build_plan(state["alert"], incident_id, datetime.now(timezone.utc))
        ctx.calllog.trace("planner", f"window {plan.window_start:%H:%M:%S}–{plan.window_end:%H:%M:%S} UTC, "
                                     f"focus: {', '.join(plan.focus_services)}")
        return {"plan": plan}

    def specialist_node(agent_module) -> Callable[[GraphState], dict]:
        def node(state: GraphState) -> dict:
            plan = state["plan"]
            task = next(t for t in plan.tasks if t.agent == agent_module.NAME)
            bundle, failure = agent_module.run(task, plan.alert, ctx)
            return {"bundles": [bundle], "failures": [failure] if failure else []}
        return node

    def diagnostician_node(state: GraphState) -> dict:
        result = diagnostician.run(state["plan"], state["bundles"], ctx)
        if isinstance(result, AgentFailure):
            return {"hypotheses": None, "failures": [result]}
        return {"hypotheses": result}

    g = StateGraph(GraphState)
    g.add_node("plan", plan_node)
    g.add_node(log_analyst.NAME, specialist_node(log_analyst))
    g.add_node(metrics_analyst.NAME, specialist_node(metrics_analyst))
    g.add_node(diagnostician.NAME, diagnostician_node)

    g.add_edge(START, "plan")
    g.add_edge("plan", log_analyst.NAME)             # fan-out: both specialists in parallel
    g.add_edge("plan", metrics_analyst.NAME)
    g.add_edge([log_analyst.NAME, metrics_analyst.NAME], diagnostician.NAME)  # fan-in: wait for both
    g.add_edge(diagnostician.NAME, END)
    return g.compile()


@dataclass
class InvestigationResult:
    run_id: str
    incident_id: str
    plan: InvestigationPlan
    bundles: list[EvidenceBundle]
    hypotheses: HypothesisSet | None
    failures: list[AgentFailure]
    log_path: Path
    result_path: Path


def investigate(alert: Alert, settings: Settings, console: Console | None = None,
                llm_factory: Callable[[Settings, CallLogger], LLM] = make_llm) -> InvestigationResult:
    """Run the whole graph for one alert and save the final state next to the call log."""
    now = datetime.now(timezone.utc)
    run_id = f"{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    incident_id = f"inc-{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
    calllog = CallLogger(settings.logs_dir / f"run_{run_id}.jsonl", run_id, incident_id, console)
    llm = llm_factory(settings, calllog)

    with httpx.Client(timeout=settings.tool_timeout_s) as http:
        ctx = RunContext(settings=settings, llm=llm, calllog=calllog, http=http)
        state = build_graph(ctx, incident_id).invoke(
            {"alert": alert, "bundles": [], "failures": []},
            config={"recursion_limit": settings.max_graph_steps},
        )

    result = InvestigationResult(
        run_id=run_id, incident_id=incident_id, plan=state["plan"],
        bundles=sorted(state["bundles"], key=lambda b: b.agent), hypotheses=state.get("hypotheses"),
        failures=state["failures"], log_path=calllog.path,
        result_path=settings.logs_dir / f"run_{run_id}_result.json",
    )
    result.result_path.write_text(json.dumps({
        "run_id": run_id, "incident_id": incident_id,
        "plan": result.plan.model_dump(mode="json"),
        "bundles": [b.model_dump(mode="json") for b in result.bundles],
        "hypotheses": result.hypotheses.model_dump(mode="json") if result.hypotheses else None,
        "failures": [f.model_dump(mode="json") for f in result.failures],
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    return result
