"""Run limits (SPEC 6.5): the time limit and the graph step limit both end in a report."""

import dataclasses
import json
import time

from langgraph.checkpoint.memory import InMemorySaver

from responder.graph import run_graph
from responder.llm import FakeLLM


class ExpiringFakeLLM(FakeLLM):
    """A fake LLM that uses up the run's time budget right after the Diagnostician answers."""
    ctx = None

    def _complete(self, agent, messages, tools, allow_tools):
        response = super()._complete(agent, messages, tools, allow_tools)
        if agent == "diagnostician" and not response.tool_calls:
            self.ctx.deadline = time.monotonic() - 1
        return response


def llm_calls(ctx) -> int:
    return sum(1 for line in ctx.calllog.path.read_text().splitlines() if json.loads(line)["call_type"] == "llm")


def test_out_of_time_from_the_start_still_writes_a_report(make_ctx, alert):
    ctx = make_ctx()
    ctx.deadline = time.monotonic() - 1
    state = run_graph(ctx, "inc-test", alert, checkpointer=None)
    report = state["report"]
    assert report.status == "failed" and "time limit" in report.limitations[0]
    assert llm_calls(ctx) == 0                       # nobody started an LLM call after the deadline


def test_out_of_time_mid_run_reports_what_exists(make_ctx, alert):
    ctx = make_ctx()
    ctx.llm = ExpiringFakeLLM(ctx.calllog)
    ctx.llm.ctx = ctx
    state = run_graph(ctx, "inc-test", alert, checkpointer=None)
    report = state["report"]
    assert state["verdicts"] == []                   # verification skipped: no time left
    assert (report.root_cause_service, report.status) == ("payment", "partial")
    assert "time limit" in report.limitations[0]


def test_step_limit_reports_from_the_last_checkpoint(make_ctx, alert):
    ctx = make_ctx()
    ctx.settings = dataclasses.replace(ctx.settings, max_graph_steps=3)  # orchestrator, specialists, diagnostician
    state = run_graph(ctx, "inc-test", alert, checkpointer=InMemorySaver())
    report = state["report"]
    assert (report.root_cause_service, report.status) == ("payment", "partial")
    assert "step limit" in report.limitations[0]
