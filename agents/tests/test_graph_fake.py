"""The prototype graph with the fake LLM: routing, structured hand-off, budgets, repair, degradation, logging."""

import dataclasses
import json

from responder.agents import log_analyst
from responder.agents.planner import build_plan
from responder.graph import build_graph
from responder.schemas import AgentFailure, EvidenceBundle, HypothesisSet

from conftest import NOW


def _run(ctx, alert):
    return build_graph(ctx, "inc-test").invoke({"alert": alert, "bundles": [], "failures": []},
                                               config={"recursion_limit": ctx.settings.max_graph_steps})


def _log_lines(ctx):
    return [json.loads(line) for line in ctx.calllog.path.read_text().splitlines()]


def test_two_specialists_feed_the_diagnostician(make_ctx, alert):
    ctx = make_ctx()
    state = _run(ctx, alert)

    assert sorted(b.agent for b in state["bundles"]) == ["log_analyst", "metrics_analyst"]
    assert all(isinstance(b, EvidenceBundle) and b.status == "ok" for b in state["bundles"])
    ids = {e.evidence_id for b in state["bundles"] for e in b.evidence}
    assert ids == {"logs-1", "metrics-1"}          # ids are assigned by code, not by the LLM
    assert isinstance(state["hypotheses"], HypothesisSet)
    assert set(state["hypotheses"].hypotheses[0].supporting_evidence) <= ids
    assert state["failures"] == []


def test_every_llm_and_tool_call_is_logged(make_ctx, alert):
    ctx = make_ctx()
    _run(ctx, alert)
    lines = _log_lines(ctx)
    by_agent = {}
    for rec in lines:
        by_agent.setdefault(rec["agent"], []).append((rec["call_type"], rec["name"], rec["status"]))
    assert by_agent["metrics_analyst"] == [("llm", "fake", "ok"), ("tool", "targets_health", "ok"),
                                           ("tool", "zscore_anomaly", "ok"), ("llm", "fake", "ok")]
    assert {"run_id", "incident_id", "latency_ms", "attempt", "tokens_in", "tokens_out", "error"} <= set(lines[0])


def test_failed_specialist_degrades_gracefully(make_ctx, alert):
    ctx = make_ctx(fail_agents={"metrics_analyst"})
    state = _run(ctx, alert)

    metrics = next(b for b in state["bundles"] if b.agent == "metrics_analyst")
    assert metrics.status == "failed" and metrics.evidence == []
    assert [f.agent for f in state["failures"]] == ["metrics_analyst"]
    assert isinstance(state["hypotheses"], HypothesisSet)   # the graph still finished
    retries = [r for r in _log_lines(ctx) if r["agent"] == "metrics_analyst"]
    assert [r["status"] for r in retries] == ["retry", "retry", "error"]   # 3 attempts, all logged


def test_invalid_json_gets_one_repair_call(make_ctx, alert):
    ctx = make_ctx(invalid_json_agents={"log_analyst"})
    plan = build_plan(alert, "inc-test", NOW)
    bundle, failure = log_analyst.run(plan.tasks[0], alert, ctx)
    assert failure is None and bundle.status == "ok"
    llm_calls = [r for r in _log_lines(ctx) if r["call_type"] == "llm"]
    assert len(llm_calls) == 3  # tool round, broken answer, repair


def test_tool_budget_is_enforced(make_ctx, alert):
    ctx = make_ctx()
    ctx.settings = dataclasses.replace(ctx.settings, max_tool_calls_per_agent=1)
    state = _run(ctx, alert)
    metrics_tools = [r for r in _log_lines(ctx) if r["agent"] == "metrics_analyst" and r["call_type"] == "tool"]
    assert len(metrics_tools) == 1   # the fake asked for 2 tools; only 1 was executed
    assert isinstance(state["hypotheses"], HypothesisSet)


def test_diagnostician_failure_is_reported(make_ctx, alert):
    ctx = make_ctx(fail_agents={"diagnostician"})
    state = _run(ctx, alert)
    assert state["hypotheses"] is None
    assert isinstance(state["failures"][-1], AgentFailure)
