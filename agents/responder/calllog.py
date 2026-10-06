"""Call logging: one JSONL line per LLM call and per tool call (SPEC 6.7).

Why it exists:
- proves the load distribution requirement (no agent > 40% of calls) -
  eval/load_report.py reads these files;
- makes every run debuggable afterwards (what was asked, how long, which errors).

`CallLogger.call()` is the single place where calls are executed with retries,
so "every call is retried and logged" is guaranteed by construction.
It also prints a short human-readable trace to the console (rich) for demos.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

from rich.console import Console
from rich.markup import escape
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

T = TypeVar("T")


class CallLogger:
    def __init__(self, path: Path, run_id: str, incident_id: str, console: Console | None = None):
        self.path = path
        self.run_id = run_id
        self.incident_id = incident_id
        self.console = console or Console(stderr=True)
        self._lock = threading.Lock()  # specialists run in parallel threads
        path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ logging
    def log(self, *, agent: str, call_type: str, name: str, latency_ms: int, status: str,
            attempt: int = 1, tokens_in: int | None = None, tokens_out: int | None = None,
            error: str | None = None) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "run_id": self.run_id, "incident_id": self.incident_id,
            "agent": agent, "call_type": call_type, "name": name,
            "latency_ms": latency_ms, "status": status, "attempt": attempt,
            "tokens_in": tokens_in, "tokens_out": tokens_out, "error": error,
        }
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def trace(self, agent: str, message: str, style: str = "") -> None:
        """Human-readable console line: `agent  → message` (message is escaped: PromQL has [1m] etc.)."""
        text = f"[{style}]{escape(message)}[/]" if style else escape(message)
        self.console.print(f"[bold cyan]{agent:>16}[/] → {text}", highlight=False)

    # ----------------------------------------------------- retried + logged call
    def call(self, *, agent: str, call_type: str, name: str, fn: Callable[[], T],
             is_retryable: Callable[[BaseException], bool], attempts: int = 3,
             usage: Callable[[T], tuple[int | None, int | None]] | None = None) -> T:
        """Run fn() with exponential-backoff retries; log every attempt.

        Each attempt produces one log line with status ok / retry / error.
        Non-retryable errors are raised immediately; after the last attempt the
        original exception is raised.
        """
        attempt_no = 0

        def one_attempt() -> T:
            nonlocal attempt_no
            attempt_no += 1
            start = time.monotonic()
            try:
                result = fn()
            except Exception as exc:  # noqa: BLE001 - we log and re-raise everything
                will_retry = is_retryable(exc) and attempt_no < attempts
                self.log(agent=agent, call_type=call_type, name=name,
                         latency_ms=_ms_since(start), status="retry" if will_retry else "error",
                         attempt=attempt_no, error=_short(exc))
                raise
            tokens_in, tokens_out = usage(result) if usage else (None, None)
            self.log(agent=agent, call_type=call_type, name=name, latency_ms=_ms_since(start),
                     status="ok", attempt=attempt_no, tokens_in=tokens_in, tokens_out=tokens_out)
            return result

        retrying = Retrying(
            stop=stop_after_attempt(attempts),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
            retry=retry_if_exception(is_retryable),
            reraise=True,
        )
        return retrying(one_attempt)  # reraise=True: the last real exception propagates


def _ms_since(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _short(exc: BaseException, limit: int = 300) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= limit else text[:limit] + "…"


def summarize(path: Path) -> dict[str, Any]:
    """Count logical calls per agent (failed attempts that were retried are not counted twice)."""
    per_agent: dict[str, dict[str, int]] = {}
    if not path.exists():
        return per_agent
    for line in path.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        if rec["status"] == "retry":
            continue
        counts = per_agent.setdefault(rec["agent"], {"llm": 0, "tool": 0})
        counts[rec["call_type"]] += 1
    return per_agent
