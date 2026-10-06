"""LLM layer: provider message pass-through, retry hints, client-side rate limit."""

import time

import httpx
import openai

from responder.llm import LLMResponse, RateLimiter, ToolCall, retry_after_seconds


def test_provider_message_is_sent_back_unchanged():
    # Gemini 3 rejects the next request if the thought_signature of a tool call is dropped.
    raw = {"role": "assistant", "tool_calls": [{
        "id": "c1", "type": "function", "function": {"name": "targets_health", "arguments": "{}"},
        "extra_content": {"google": {"thought_signature": "SIG"}}}]}
    resp = LLMResponse(None, [ToolCall("c1", "targets_health", "{}")], raw_message=raw)
    assert resp.as_message()["tool_calls"][0]["extra_content"] == {"google": {"thought_signature": "SIG"}}


def test_message_is_built_when_there_is_no_raw_message():
    msg = LLMResponse(None, [ToolCall("c1", "dependency_graph", "{}")]).as_message()
    assert msg["role"] == "assistant" and msg["tool_calls"][0]["id"] == "c1"


def test_retry_hint_from_gemini_quota_error_text():
    exc = RuntimeError("Error code: 429 - Quota exceeded ... Please retry in 30.925607081s.")
    assert retry_after_seconds(exc) == 30.925607081
    assert retry_after_seconds(RuntimeError("model overloaded")) is None


def test_retry_hint_from_retry_after_header():
    response = httpx.Response(429, headers={"retry-after": "12"}, request=httpx.Request("POST", "http://x"))
    exc = openai.RateLimitError("rate limited", response=response, body=None)
    assert retry_after_seconds(exc) == 12.0


def test_rate_limiter_blocks_when_the_minute_is_full():
    limiter = RateLimiter(max_per_minute=2)
    assert limiter.acquire() == 0 and limiter.acquire() == 0
    limiter._sent[0] -= 59.8          # pretend the first request was sent ~a minute ago
    start = time.monotonic()
    waited = limiter.acquire()        # third request must wait for the window to free up
    assert 0 < waited < 1 and time.monotonic() - start < 1


def test_rate_limiter_disabled_by_default():
    limiter = RateLimiter(max_per_minute=0)
    assert all(limiter.acquire() == 0 for _ in range(100))
