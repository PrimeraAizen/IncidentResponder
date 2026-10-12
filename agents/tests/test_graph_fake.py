"""The full 7-agent graph with the fake LLM: routing, structured hand-off, verify loop,
loop protection, budgets, repair, degradation, state store, checkpointer and call logging."""

import copy
import dataclasses
import json
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from responder.agents import log_analyst
from responder.agents.planner import build_plan
from responder.graph import build_graph
from responder.llm import FAKE_SCRIPTS
from responder.state_store import StateStore

from conftest import NOW

PAYMENT = {"hypothesis_id": "h1", "root_cause_service": "payment", "fault_type": "latency", "explanation": "x",
           "supporting_evidence": ["metrics-1"], "suspect_commit": None, "confidence": 0.7}
ORDER = {**PAYMENT, "root_cause_service": "order", "fault_type": "bad_deploy", "suspect_commit": "abc1234"}


def verdict(status: str) -> dict:
    return {**FAKE_SCRIPTS["verifier"]["final"], "status": status}


def scripts(diagnostician=None, verifier=None) -> dict:
    """Copy of the default fake scripts with different final answers for some agents."""
    s = copy.deepcopy(FAKE_SCRIPTS)
    if diagnostician:
        s["diagnostician"]["final"] = [{"hypotheses": [h]} for h in diagnostician]
    if verifier:
        s["verifier"]["final"] = [verdict(v) for v in verifier]
    return s


def run(ctx, alert, checkpointer=None):
    return build_graph(ctx, "inc-test", checkpointer).invoke(
        {"alert": alert, "bundles": [], "failures": [], "verdicts": [], "rejected": []},
        config={"recursion_limit": ctx.settings.max_graph_steps, "configurable": {"thread_id": "inc-test"}})


def log_lines(ctx):
    return [json.loads(line) for line in ctx.calllog.path.read_text().splitlines()]


def test_full_graph_happy_path(make_ctx, alert, tmp_path):
    store = StateStore(tmp_path / "incidents.db")
    store.start_incident("inc-test", "run-test", alert)
    ctx = make_ctx(store=store)
    with SqliteSaver.from_conn_string(str(tmp_path / "checkpoints.db")) as checkpointer:
        state = run(ctx, alert, checkpointer)
        assert list(checkpointer.list({"configurable": {"thread_id": "inc-test"}}))  # graph state was checkpointed

    assert state["plan"].focus_services[:2] == ["gateway", "payment"]          # from the orchestrator
    assert sorted(b.agent for b in state["bundles"]) == ["change_investigator", "log_analyst", "metrics_analyst"]
    ids = {e.evidence_id for b in state["bundles"] for e in b.evidence}
    assert ids == {"logs-1", "metrics-1", "changes-1"}                          # ids assigned by code
    assert [v.status for v in state["verdicts"]] == ["confirmed"]
    report = state["report"]
    assert (report.root_cause_service, report.fault_type, report.status) == ("payment", "latency", "complete")
    assert report.confidence == 0.8                                            # 0.7 + 0.1 for a confirmed verdict
    assert Path(state["report_paths"]["md_path"]).read_text().startswith("# Incident inc-test: payment / latency")

    saved = store.load("inc-test")
    assert {k: len(v) for k, v in saved.items()} == {"incidents": 1, "plans": 1, "evidence": 3,
                                                     "hypotheses": 1, "verdicts": 1, "reports": 1}


def test_every_llm_and_tool_call_is_logged(make_ctx, alert):
    ctx = make_ctx()
    run(ctx, alert)
    by_agent: dict[str, list] = {}
    for rec in log_lines(ctx):
        by_agent.setdefault(rec["agent"], []).append((rec["call_type"], rec["name"]))
    assert by_agent["orchestrator"] == [("llm", "fake")]                       # 1 LLM call, no tools
    assert by_agent["metrics_analyst"] == [("llm", "fake"), ("tool", "targets_health"),
                                           ("tool", "zscore_anomaly"), ("llm", "fake")]
    assert by_agent["reporter"] == [("llm", "fake"), ("tool", "write_report")]
    assert set(by_agent) == {"orchestrator", "log_analyst", "metrics_analyst", "change_investigator",
                             "diagnostician", "verifier", "reporter"}


def test_rejected_hypothesis_goes_back_to_the_diagnostician(make_ctx, alert):
    ctx = make_ctx(scripts=scripts(diagnostician=[PAYMENT, ORDER], verifier=["rejected", "confirmed"]))
    state = run(ctx, alert)
    assert [v.status for v in state["verdicts"]] == ["rejected", "confirmed"]
    assert (state["report"].root_cause_service, state["report"].suspect_commit) == ("order", "abc1234")


def test_re_proposing_a_rejected_hypothesis_ends_the_loop(make_ctx, alert):
    ctx = make_ctx(scripts=scripts(diagnostician=[PAYMENT], verifier=["rejected"]))
    state = run(ctx, alert)
    assert len(state["verdicts"]) == 1                     # no second verification of the same hypothesis
    assert state["report"].status == "partial"
    assert state["report"].confidence == 0.35              # 0.7 halved: the Verifier rejected it


def test_verify_loop_is_bounded(make_ctx, alert):
    third = {**PAYMENT, "root_cause_service": "inventory"}
    ctx = make_ctx(scripts=scripts(diagnostician=[PAYMENT, ORDER, third], verifier=["rejected"]))
    state = run(ctx, alert)
    assert len(state["verdicts"]) == ctx.settings.max_verify_loops == 2
    assert state["report"].root_cause_service == "order"


def test_failed_specialist_degrades_gracefully(make_ctx, alert):
    ctx = make_ctx(fail_agents={"metrics_analyst"})
    state = run(ctx, alert)
    metrics = next(b for b in state["bundles"] if b.agent == "metrics_analyst")
    assert metrics.status == "failed" and metrics.evidence == []
    report = state["report"]
    assert report.status == "partial" and report.confidence == 0.64    # (0.7 + 0.1) * 0.8
    assert any("metrics_analyst" in item for item in report.limitations)
    retries = [r["status"] for r in log_lines(ctx) if r["agent"] == "metrics_analyst"]
    assert retries == ["retry", "retry", "error"]                         # 3 attempts, all logged


def test_orchestrator_failure_falls_back_to_the_deterministic_plan(make_ctx, alert):
    state = run(make_ctx(fail_agents={"orchestrator"}), alert)
    assert len(state["plan"].tasks) == 3 and state["report"].root_cause_service == "payment"
    assert [f.agent for f in state["failures"]] == ["orchestrator"]


def test_diagnostician_failure_produces_a_failed_report(make_ctx, alert):
    state = run(make_ctx(fail_agents={"diagnostician"}), alert)
    assert state["verdicts"] == []
    assert (state["report"].status, state["report"].root_cause_service) == ("failed", "unknown")


def test_reporter_failure_still_writes_a_report(make_ctx, alert):
    state = run(make_ctx(fail_agents={"reporter"}), alert)
    report = state["report"]
    assert report.root_cause_service == "payment" and Path(state["report_paths"]["md_path"]).exists()


def test_invalid_json_gets_one_repair_call(make_ctx, alert):
    ctx = make_ctx(invalid_json_agents={"log_analyst"})
    plan = build_plan(alert, "inc-test", NOW)
    bundle, failure = log_analyst.run(plan.tasks[0], alert, ctx)
    assert failure is None and bundle.status == "ok"
    assert len([r for r in log_lines(ctx) if r["call_type"] == "llm"]) == 3  # tool round, broken answer, repair


def test_tool_budget_is_enforced(make_ctx, alert):
    ctx = make_ctx()
    ctx.settings = dataclasses.replace(ctx.settings, max_tool_calls_per_agent=1)
    state = run(ctx, alert)
    metrics_tools = [r for r in log_lines(ctx) if r["agent"] == "metrics_analyst" and r["call_type"] == "tool"]
    assert len(metrics_tools) == 1   # the fake asked for 2 tools; only 1 was executed
    assert state["report"] is not None
