"""Shared test fixtures: settings with the fake LLM, and a mocked Prometheus/Loki HTTP API.

The mock transport lets the REAL tool code run (parsing, summarizing) without a live stack.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from responder.calllog import CallLogger
from responder.config import Settings
from responder.llm import FakeLLM
from responder.runtime import RunContext
from responder.schemas import Alert

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return dataclasses.replace(Settings.from_env(), llm_provider="fake", logs_dir=tmp_path,
                               max_tool_calls_per_agent=4, max_llm_calls_per_agent=3, retry_attempts=3)


@pytest.fixture
def alert() -> Alert:
    return Alert(alert_name="HighLatency", service="gateway", severity="critical",
                 started_at=NOW - timedelta(minutes=2), summary="p95 latency of POST /api/orders > 1s")


def fake_observability(request: httpx.Request) -> httpx.Response:
    """Minimal Prometheus + Loki API: one latency series that jumps from 0.06 s to 2.05 s."""
    path, params = request.url.path, request.url.params
    if path == "/api/v1/targets":
        return httpx.Response(200, json={"status": "success", "data": {"activeTargets": [
            {"labels": {"service": s, "job": s}, "health": "up", "lastError": "", "lastScrape": "2026-10-06T12:00:00Z"}
            for s in ("gateway", "order", "payment")]}})
    if path == "/api/v1/query_range":
        start, end = float(params["start"]), float(params["end"])
        jump = (NOW - timedelta(minutes=2, seconds=30)).timestamp()
        values, t = [], start
        while t <= end:
            values.append([t, "2.05" if t >= jump else "0.06"])
            t += 15
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix", "result": [
            {"metric": {"service": "gateway", "target": "payment"}, "values": values}]}})
    if path == "/api/v1/query":
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": [
            {"metric": {"service": "gateway"}, "value": [NOW.timestamp(), "1"]}]}})
    if path == "/loki/api/v1/query_range":
        ts = int((NOW - timedelta(minutes=1)).timestamp() * 1e9)
        line = json.dumps({"level": "WARN", "msg": "slow upstream response", "service": "gateway",
                           "upstream": "payment", "route": "/charge", "latency_ms": 2051})
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "streams", "result": [
            {"stream": {"service": "gateway"}, "values": [[str(ts + i), line] for i in range(5)]}]}})
    return httpx.Response(404, text="not mocked")


@pytest.fixture
def http() -> httpx.Client:
    with httpx.Client(transport=httpx.MockTransport(fake_observability)) as client:
        yield client


@pytest.fixture
def make_ctx(settings, http, tmp_path):
    """Factory: RunContext with a FakeLLM (optionally failing / returning invalid JSON)."""
    def factory(**fake_kwargs) -> RunContext:
        calllog = CallLogger(tmp_path / "run_test.jsonl", "run-test", "inc-test")
        return RunContext(settings=settings, llm=FakeLLM(calllog, **fake_kwargs), calllog=calllog, http=http)
    return factory
