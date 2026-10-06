"""Pure logic inside the tools: log pattern grouping, change point, z-score, schema simplification."""

import json

from pydantic import BaseModel

from responder.llm import extract_json
from responder.tools.base import _simplify_schema
from responder.tools.loki import line_pattern, normalize, summarize_patterns
from responder.tools.prometheus import change_point, zscore


def test_normalize_strips_variable_parts():
    assert normalize("order 123 failed for 10.0.0.5:5432 id 9f8e7d6c5b4a") == "order <n> failed for <ip> id <hex>"


def test_json_lines_with_different_ids_share_a_pattern():
    a = json.dumps({"level": "ERROR", "msg": "database query failed", "upstream": "postgres",
                    "error": "dial tcp 172.20.0.2:5432: connect: connection refused", "trace_id": "aaa"})
    b = json.dumps({"level": "ERROR", "msg": "database query failed", "upstream": "postgres",
                    "error": "dial tcp 172.20.0.7:5432: connect: connection refused", "trace_id": "bbb"})
    assert line_pattern(a) == line_pattern(b)
    assert "upstream=postgres" in line_pattern(a)


def test_plain_text_lines_drop_leading_timestamp():
    line = "2026-10-06 12:00:01.123 UTC [1] FATAL:  the database system is shutting down"
    assert line_pattern(line) == "[<n>] FATAL: the database system is shutting down"


def test_summarize_patterns_counts_and_orders():
    warn = json.dumps({"level": "WARN", "msg": "slow upstream response", "upstream": "payment"})
    err = json.dumps({"level": "ERROR", "msg": "upstream request failed", "upstream": "order"})
    entries = [(3_000_000_000, "gateway", warn), (1_000_000_000, "gateway", warn), (2_000_000_000, "gateway", err)]
    patterns = summarize_patterns(entries)
    assert [p["count"] for p in patterns] == [2, 1]
    assert patterns[0]["service"] == "gateway"
    assert patterns[0]["first_seen"] < patterns[0]["last_seen"]


def test_change_point_finds_the_jump():
    k, before, after = change_point([0.06] * 10 + [2.0] * 6)
    assert k == 10 and before == 0.06 and after == 2.0
    assert change_point([1.0, 2.0]) is None


def test_zscore_detects_shift_and_floors_tiny_std():
    result = zscore([0.06] * 20, [2.0] * 5)
    assert result["zscore"] == 100.0           # capped
    assert result["ratio"] == round(2.0 / 0.06, 2)
    assert zscore([1.0, 1.1, 0.9], [1.0, 1.05])["zscore"] < 1  # normal noise stays small


def test_zscore_without_baseline():
    result = zscore([], [3.0])
    assert result["zscore"] is None and result["incident_mean"] == 3.0


def test_optional_fields_are_simplified_for_providers():
    class Args(BaseModel):
        promql: str
        at: str | None = None

    schema = _simplify_schema(Args.model_json_schema())
    assert schema["properties"]["at"] == {"type": "string"}
    assert schema["required"] == ["promql"]
    assert "title" not in json.dumps(schema)


def test_extract_json_tolerates_fences_and_prose():
    assert extract_json('Here you go:\n```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('result: {"a": {"b": 2}} done') == {"a": {"b": 2}}
