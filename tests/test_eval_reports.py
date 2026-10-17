"""Load-report math and the evaluation summary (pure functions)."""

from eval.load_report import agent_shares, replace_section
from eval.run_all import summarize, typical_run


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
