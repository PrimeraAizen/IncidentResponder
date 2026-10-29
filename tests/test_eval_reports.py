"""Load-report math and the evaluation summary (pure functions)."""

from eval.load_report import agent_shares, replace_section
from eval.run_all import summarize, typical_run
from eval.run_scenario import provider_failure
from responder.schemas import AgentFailure


def call(agent: str, kind: str = "llm", status: str = "ok") -> dict:
    return {"agent": agent, "call_type": kind, "status": status}


def test_agent_shares_count_retried_attempts_once():
    records = [call("a"), call("a", "tool"), call("a", "tool", "retry"), call("a", "tool"),
               call("b"), call("b", "tool"), call("c")]
    rows = {r["agent"]: r for r in agent_shares(records)}
    assert (rows["a"]["llm"], rows["a"]["tool"], rows["a"]["total"]) == (1, 2, 3)
    assert round(rows["a"]["share"], 2) == 0.5 and not rows["a"]["ok"]   # 3 of 6 calls > 40%
    assert rows["c"]["ok"] and abs(sum(r["share"] for r in rows.values()) - 1) < 1e-9


def test_replace_section_is_idempotent():
    text = "intro\n<!-- x:start -->\nold\n<!-- x:end -->\nend"
    once = replace_section(text, "x", "new")
    assert "old" not in once and "new" in once and once.endswith("end")
    assert replace_section(once, "x", "new") == once
    assert replace_section("no markers", "y", "body").endswith("<!-- y:end -->\n")


def record(scenario: str, correct: bool, calls: int, commit_ok=None) -> dict:
    return {"scenario": scenario, "repeat": 1, "expected": {"root_cause_service": "payment", "fault_type": "latency"},
            "report": {"root_cause_service": "payment", "fault_type": "latency"},
            "score": {"correct": correct, "commit_expected": commit_ok is not None, "commit_ok": commit_ok},
            "symptoms_ok": True, "investigation_s": 60.0, "llm_calls": calls, "tool_calls": calls,
            "tokens_in": 1000, "tokens_out": 100}


def test_summary_uses_the_majority_of_repeats():
    rows = {r["scenario"]: r for r in summarize([record("s01", True, 10), record("s01", True, 12),
                                                 record("s01", False, 11), record("s04", False, 9, False),
                                                 record("s04", True, 9, True)])}
    assert rows["s01"]["correct"] == 2 and rows["s01"]["majority"]
    assert not rows["s04"]["majority"] and rows["s04"]["commit"] == "1/2"   # 1 of 2 is not a majority


def test_typical_run_is_a_correct_run_with_median_calls():
    records = [record("s01", True, 10), record("s02", True, 14), record("s03", True, 20), record("s04", False, 12)]
    assert typical_run(records)["scenario"] == "s02"


def test_run_dates_span_resumed_evaluations():
    from eval.run_all import run_dates
    assert run_dates([{"run_id": "20261017T131529Z-ab"}, {"run_id": "20261027T090000Z-cd"}]) == "2026-10-17 … 2026-10-27"
    assert run_dates([{"run_id": "20261017T131529Z-ab"}]) == "2026-10-17"


def test_pending_runs_skip_saved_results_in_round_robin_order(tmp_path):
    from eval.run_all import pending_runs
    (tmp_path / "s01_payment_latency_1.json").write_text("{}")
    (tmp_path / "s02_postgres_down_1.json").write_text("{}")
    assert pending_runs(["s01", "s02", "s03"], 2, tmp_path) == [("s03", 1), ("s01", 2), ("s02", 2), ("s03", 2)]


def test_provider_failures_stop_the_evaluation_but_agent_failures_do_not():
    def failed(error: str) -> AgentFailure:
        return AgentFailure(agent="log_analyst", error=error, attempts=3)

    down = failed("LLM call failed (gemini-3.1-flash-lite): Connection error.")
    quota = failed("Error code: 429 - You exceeded your current quota, please check your plan")
    busy = failed("LLM call failed (gemini-3.1-flash-lite): Error code: 503 - This model is currently experiencing high demand")
    slow = failed("LLM call failed (gemini-3.1-flash-lite): Request timed out.")
    agent = failed("invalid JSON in the final answer")
    assert provider_failure([agent, down]) == down.error
    assert all(provider_failure([f]) == f.error for f in (quota, busy, slow))
    assert provider_failure([agent]) is None and provider_failure([]) is None
