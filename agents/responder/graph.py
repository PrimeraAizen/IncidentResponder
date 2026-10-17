"""LangGraph wiring of the 7 agents. Routing is plain code (edges), never an LLM call.

    START → orchestrator ─┬─► log_analyst ─────────┐
                          ├─► metrics_analyst ─────┼─► diagnostician ─► verifier ─► reporter → END
                          └─► change_investigator ─┘        ▲              │
                                                            └── rejected ──┘
                                                          (at most MAX_VERIFY_LOOPS)

- The three specialists run in parallel; the Diagnostician waits for all of them.
- After the Verifier: a rejected hypothesis goes back to the Diagnostician while
  verify_loops < MAX_VERIFY_LOOPS; otherwise (confirmed / inconclusive / limit) to the Reporter.
- Loop protection: if the Diagnostician re-proposes a hypothesis the Verifier
  already rejected, it is treated as rejected-final and the Reporter takes over.
- If the Diagnostician fails, the Reporter writes a "failed" report.
- Limits: once RUN_TIMEOUT_S has passed, every route leads to the Reporter, which
  writes a "partial" report from whatever exists. If LangGraph's step limit
  (MAX_GRAPH_STEPS) aborts the run, the last checkpointed state goes to the Reporter.

State: GraphState below (explicit, typed). Every node also saves its output in
the SQLite StateStore; the LangGraph SqliteSaver checkpoints the graph state.
"""

from __future__ import annotations

import json
import operator
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Callable, TypedDict

import httpx
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from rich.console import Console

from responder.agents import (change_investigator, diagnostician, log_analyst, metrics_analyst, orchestrator,
                              reporter, verifier)
from responder.agents.planner import build_plan
from responder.calllog import CallLogger
from responder.config import Settings
from responder.llm import LLM, make_llm
from responder.runtime import RunContext
from responder.schemas import (AgentFailure, Alert, EvidenceBundle, Hypothesis, HypothesisSet, IncidentReport,
                               InvestigationPlan, Verdict)
from responder.state_store import StateStore

SPECIALISTS = (log_analyst, metrics_analyst, change_investigator)


class GraphState(TypedDict, total=False):
    """Explicit task state passed between nodes."""
    alert: Alert
    plan: InvestigationPlan
    bundles: Annotated[list[EvidenceBundle], operator.add]   # parallel specialists append here
    failures: Annotated[list[AgentFailure], operator.add]
    hypotheses: HypothesisSet | None                         # the Diagnostician's latest output
    verdicts: Annotated[list[Verdict], operator.add]
    tested: Annotated[list[Hypothesis], operator.add]        # tested[i] = the hypothesis verdicts[i] judged
    last_verified: Hypothesis | None                         # the hypothesis the last verdict is about
    rejected: Annotated[list[str], operator.add]             # "service/fault_type" keys the Verifier rejected
    verify_loops: int
    diagnostician_failed: bool
    report: IncidentReport | None
    report_paths: dict


def key(h: Hypothesis) -> str:
    return f"{h.root_cause_service}/{h.fault_type}"


def build_graph(ctx: RunContext, incident_id: str, checkpointer=None):
    store = ctx.store

    def orchestrator_node(state: GraphState) -> dict:
        plan, failure = orchestrator.run(state["alert"], incident_id, ctx)
        if store:
            store.save_plan(incident_id, plan)
        return {"plan": plan, "failures": [failure] if failure else [], "verify_loops": 0}

    def specialist_node(agent_module) -> Callable[[GraphState], dict]:
        def node(state: GraphState) -> dict:
            plan = state["plan"]
            task = next(t for t in plan.tasks if t.agent == agent_module.NAME)
            bundle, failure = agent_module.run(task, plan.alert, ctx)
            if store:
                store.save_evidence(incident_id, bundle)
            return {"bundles": [bundle], "failures": [failure] if failure else []}
        return node

    def diagnostician_node(state: GraphState) -> dict:
        verdicts = state.get("verdicts") or []
        result = diagnostician.run(state["plan"], state["bundles"], ctx,
                                   previous_verdict=verdicts[-1] if verdicts else None,
                                   rejected=state.get("rejected") or [])
        if isinstance(result, AgentFailure):  # keep earlier hypotheses (if any): they are still the best we have
            return {"failures": [result], "diagnostician_failed": True}
        if store:
            store.save_hypotheses(incident_id, state.get("verify_loops", 0), result)
        return {"hypotheses": result, "diagnostician_failed": False}

    def verifier_node(state: GraphState) -> dict:
        top = state["hypotheses"].hypotheses[0]
        loops = state.get("verify_loops", 0) + 1
        result = verifier.run(top, state["plan"], ctx)
        if isinstance(result, AgentFailure):
            return {"verify_loops": loops, "failures": [result], "last_verified": None}
        if store:
            store.save_verdict(incident_id, loops, result)
        update = {"verify_loops": loops, "verdicts": [result], "tested": [top], "last_verified": top}
        if result.status == "rejected":
            update["rejected"] = [key(top)]
        return update

    def reporter_node(state: GraphState) -> dict:
        report, paths, failure = reporter.run(
            state["plan"], state.get("bundles") or [], state.get("hypotheses"), state.get("verdicts") or [],
            state.get("tested") or [], state.get("failures") or [], ctx)
        if store:
            store.save_report(incident_id, report, paths.get("md_path"))
        return {"report": report, "report_paths": paths, "failures": [failure] if failure else []}

    # ---- routing (code, not LLM) -------------------------------------------------
    def after_diagnostician(state: GraphState) -> str:
        hypotheses = state.get("hypotheses")
        if hypotheses is None or state.get("diagnostician_failed"):
            return "reporter"
        if ctx.time_left() <= 0:
            ctx.calllog.trace("graph", "time limit reached → reporter (no verification)", "yellow")
            return "reporter"
        if key(hypotheses.hypotheses[0]) in (state.get("rejected") or []):
            ctx.calllog.trace("graph", "re-proposed an already rejected hypothesis → reporter (loop protection)", "yellow")
            return "reporter"
        return "verifier"

    def after_verifier(state: GraphState) -> str:
        verdicts = state.get("verdicts") or []
        rejected_now = bool(verdicts) and state.get("last_verified") is not None and verdicts[-1].status == "rejected"
        if rejected_now and ctx.time_left() <= 0:
            ctx.calllog.trace("graph", "time limit reached → reporter (no further loop)", "yellow")
            return "reporter"
        if rejected_now and state.get("verify_loops", 0) < ctx.settings.max_verify_loops:
            ctx.calllog.trace("graph", f"hypothesis rejected → back to the diagnostician "
                                       f"(loop {state['verify_loops']}/{ctx.settings.max_verify_loops})", "yellow")
            return "diagnostician"
        return "reporter"

    g = StateGraph(GraphState)
    g.add_node(orchestrator.NAME, orchestrator_node)
    for agent in SPECIALISTS:
        g.add_node(agent.NAME, specialist_node(agent))
    g.add_node(diagnostician.NAME, diagnostician_node)
    g.add_node(verifier.NAME, verifier_node)
    g.add_node(reporter.NAME, reporter_node)

    g.add_edge(START, orchestrator.NAME)
    for agent in SPECIALISTS:                                        # fan-out: specialists in parallel
        g.add_edge(orchestrator.NAME, agent.NAME)
    g.add_edge([a.NAME for a in SPECIALISTS], diagnostician.NAME)    # fan-in: wait for all three
    g.add_conditional_edges(diagnostician.NAME, after_diagnostician, [verifier.NAME, reporter.NAME])
    g.add_conditional_edges(verifier.NAME, after_verifier, [diagnostician.NAME, reporter.NAME])
    g.add_edge(reporter.NAME, END)
    return g.compile(checkpointer=checkpointer)


def run_graph(ctx: RunContext, incident_id: str, alert: Alert, checkpointer) -> dict:
    """Invoke the graph; if the step limit aborts it, report from the last checkpointed state."""
    graph = build_graph(ctx, incident_id, checkpointer)
    config = {"recursion_limit": ctx.settings.max_graph_steps, "configurable": {"thread_id": incident_id}}
    try:
        return graph.invoke({"alert": alert, "bundles": [], "failures": [], "verdicts": [], "tested": [],
                             "rejected": []}, config)
    except GraphRecursionError:
        state = dict(graph.get_state(config).values)
        reason = f"graph step limit reached (MAX_GRAPH_STEPS={ctx.settings.max_graph_steps})"
        ctx.calllog.trace("graph", f"{reason} → reporter", "red")
        if "plan" not in state:  # aborted before the orchestrator finished
            state["plan"] = build_plan(alert, incident_id, datetime.now(timezone.utc))
        report, paths, _ = reporter.run(state["plan"], state.get("bundles") or [], state.get("hypotheses"),
                                        state.get("verdicts") or [], state.get("tested") or [],
                                        state.get("failures") or [], ctx, stopped_early=reason)
        if ctx.store:
            ctx.store.save_report(incident_id, report, paths.get("md_path"))
        return {**state, "report": report, "report_paths": paths}


@dataclass
class InvestigationResult:
    run_id: str
    incident_id: str
    plan: InvestigationPlan
    bundles: list[EvidenceBundle]
    hypotheses: HypothesisSet | None
    verdicts: list[Verdict]
    report: IncidentReport | None
    report_paths: dict
    failures: list[AgentFailure]
    log_path: Path
    result_path: Path


def investigate(alert: Alert, settings: Settings, console: Console | None = None,
                llm_factory: Callable[[Settings, CallLogger], LLM] = make_llm) -> InvestigationResult:
    """Run the whole graph for one alert; save the final state next to the call log."""
    now = datetime.now(timezone.utc)
    run_id = f"{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    incident_id = f"inc-{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
    calllog = CallLogger(settings.logs_dir / f"run_{run_id}.jsonl", run_id, incident_id, console)
    llm = llm_factory(settings, calllog)
    store = StateStore(settings.state_db_path)
    store.start_incident(incident_id, run_id, alert)

    with httpx.Client(timeout=settings.tool_timeout_s) as http, \
            SqliteSaver.from_conn_string(str(settings.checkpoint_db_path)) as checkpointer:
        ctx = RunContext(settings=settings, llm=llm, calllog=calllog, http=http, store=store,
                         deadline=time.monotonic() + settings.run_timeout_s)
        state = run_graph(ctx, incident_id, alert, checkpointer)
    store.close()

    result = InvestigationResult(
        run_id=run_id, incident_id=incident_id, plan=state["plan"],
        bundles=sorted(state["bundles"], key=lambda b: b.agent), hypotheses=state.get("hypotheses"),
        verdicts=state.get("verdicts") or [], report=state.get("report"),
        report_paths=state.get("report_paths") or {}, failures=state.get("failures") or [],
        log_path=calllog.path, result_path=settings.logs_dir / f"run_{run_id}_result.json",
    )
    result.result_path.write_text(json.dumps({
        "run_id": run_id, "incident_id": incident_id,
        "plan": result.plan.model_dump(mode="json"),
        "bundles": [b.model_dump(mode="json") for b in result.bundles],
        "hypotheses": result.hypotheses.model_dump(mode="json") if result.hypotheses else None,
        "verdicts": [v.model_dump(mode="json") for v in result.verdicts],
        "report": result.report.model_dump(mode="json") if result.report else None,
        "report_paths": result.report_paths,
        "failures": [f.model_dump(mode="json") for f in result.failures],
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    return result
