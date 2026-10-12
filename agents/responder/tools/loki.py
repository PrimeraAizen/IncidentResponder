"""Loki tools (Log Analyst, Verifier).

error_pattern_summary does the heavy lifting in plain Python: it fetches
warning/error lines, normalizes away ids/numbers ("order 123 failed" and
"order 456 failed" become one pattern) and groups them with counts and
first/last-seen times. The LLM reads ~10 patterns instead of thousands of lines.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from responder.schemas import UTCDateTime
from responder.tools.base import Tool, ToolContext, ToolError

MAX_LINES_RETURNED = 40
PATTERN_FETCH_LIMIT = 5000  # Loki max_entries_limit_per_query
# Warnings/errors in our JSON logs, plus typical failure words in plain-text logs (postgres, nats, ...).
WARN_ERROR_FILTER = r'|~ "(?i)(\"level\":\"(warn|error)\"|error|fatal|panic|timeout|refused)"'
_SERVICE_RE = re.compile(r"^[a-z0-9_-]+$")
# Fields worth keeping when a JSON log line is shown to the LLM.
_KEEP_FIELDS = ("level", "msg", "route", "method", "status", "latency_ms", "upstream", "query", "error",
                "location", "attempt")


# ------------------------------------------------------------------- HTTP helper
def _loki(ctx: ToolContext, path: str, params: dict[str, Any]) -> Any:
    resp = ctx.http.get(f"{ctx.settings.loki_url}{path}", params=params)
    if resp.status_code >= 500:
        resp.raise_for_status()  # retryable
    if resp.status_code >= 400:
        raise ToolError(f"loki rejected the query: {resp.text.strip()[:300]}")
    return resp.json()["data"]


def _ns(dt: datetime) -> int:
    return int(dt.timestamp() * 1e9)


def _iso_ns(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch_lines(ctx: ToolContext, logql: str, start: datetime, end: datetime,
                 limit: int) -> list[tuple[int, str, str]]:
    """Return [(ts_ns, service, line)] newest first."""
    data = _loki(ctx, "/loki/api/v1/query_range",
                 {"query": logql, "start": _ns(start), "end": _ns(end), "limit": limit, "direction": "backward"})
    if data["resultType"] != "streams":
        raise ToolError("expected a log query, got a metric query; use prom_* tools for metrics")
    lines = [(int(ts), stream["stream"].get("service", "?"), line)
             for stream in data["result"] for ts, line in stream["values"]]
    lines.sort(key=lambda x: x[0], reverse=True)
    return lines[:limit]


# --------------------------------------------------------------- pure functions
def compact_line(line: str, limit: int = 220) -> str:
    """Shorten a log line: JSON lines keep only the interesting fields."""
    try:
        obj = json.loads(line)
    except ValueError:
        obj = None
    if isinstance(obj, dict):
        line = " ".join(f"{k}={obj[k]}" for k in _KEEP_FIELDS if k in obj)
    return line if len(line) <= limit else line[:limit] + "…"


def normalize(text: str) -> str:
    """Replace variable parts (ids, IPs, numbers) so similar messages group together."""
    text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "<uuid>", text, flags=re.I)
    text = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b", "<ip>", text)
    text = re.sub(r"\b(?=[0-9a-f]*\d)[0-9a-f]{8,}\b", "<hex>", text, flags=re.I)
    text = re.sub(r"\d+(?:\.\d+)?", "<n>", text)
    return re.sub(r"\s+", " ", text).strip()


_PLAIN_TS = re.compile(r"^\S*\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?\s*(?:UTC|Z)?\s*")


def line_pattern(line: str) -> str:
    """The grouping key of one log line."""
    try:
        obj = json.loads(line)
    except ValueError:
        obj = None
    if isinstance(obj, dict):
        parts = [str(obj.get("level", "")), normalize(str(obj.get("msg", "")))]
        for key in ("upstream", "route", "status", "query"):  # kept verbatim: low cardinality
            if key in obj:
                parts.append(f"{key}={obj[key]}")
        if "error" in obj:
            parts.append(f"error={normalize(str(obj['error']))}")
        return " ".join(p for p in parts if p)
    return normalize(_PLAIN_TS.sub("", line))[:200]


def summarize_patterns(entries: list[tuple[int, str, str]], top: int = 10) -> list[dict]:
    """Group (ts_ns, service, line) entries by (service, pattern); most frequent first."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for ts, service, line in entries:
        key = (service, line_pattern(line))
        g = groups.setdefault(key, {"count": 0, "first": ts, "last": ts, "example": line})
        g["count"] += 1
        g["first"], g["last"] = min(g["first"], ts), max(g["last"], ts)
    ranked = sorted(groups.items(), key=lambda kv: kv[1]["count"], reverse=True)[:top]
    return [{"service": svc, "pattern": pattern, "count": g["count"],
             "first_seen": _iso_ns(g["first"]), "last_seen": _iso_ns(g["last"]),
             "example": compact_line(g["example"])}
            for (svc, pattern), g in ranked]


# -------------------------------------------------------------------- tools
class LokiQueryArgs(BaseModel):
    logql: str = Field(description='LogQL log query, e.g. {service="order"} | json | level="ERROR"')
    start: UTCDateTime | None = Field(None, description="Default: start of the investigation window")
    end: UTCDateTime | None = Field(None, description="Default: end of the investigation window")
    limit: int = Field(100, ge=1, le=1000, description="Max lines to fetch (newest first)")


def loki_query(args: LokiQueryArgs, ctx: ToolContext) -> dict:
    start, end = ctx.window(args.start, args.end)
    lines = _fetch_lines(ctx, args.logql, start, end, args.limit)
    shown = lines[:MAX_LINES_RETURNED]
    return {"logql": args.logql, "fetched": len(lines), "shown": len(shown),
            "lines": [{"ts": _iso_ns(ts), "service": svc, "line": compact_line(line)} for ts, svc, line in shown]}


class LabelValuesArgs(BaseModel):
    label: str = Field("service", description="Label name, e.g. service or container")


def loki_label_values(args: LabelValuesArgs, ctx: ToolContext) -> dict:
    data = _loki(ctx, f"/loki/api/v1/label/{args.label}/values",
                 {"start": _ns(ctx.window_start), "end": _ns(ctx.window_end)})
    return {"label": args.label, "values": sorted(data or [])}


class ErrorPatternArgs(BaseModel):
    service: str | None = Field(None, description="Service to analyze; omit to cover all services")
    start: UTCDateTime | None = Field(None, description="Default: start of the investigation window")
    end: UTCDateTime | None = Field(None, description="Default: end of the investigation window")


def error_pattern_summary(args: ErrorPatternArgs, ctx: ToolContext) -> dict:
    if args.service and not _SERVICE_RE.match(args.service):
        raise ToolError(f"invalid service name {args.service!r}")
    start, end = ctx.window(args.start, args.end)
    selector = f'{{service="{args.service}"}}' if args.service else '{service=~".+"}'
    lines = _fetch_lines(ctx, f"{selector} {WARN_ERROR_FILTER}", start, end, PATTERN_FETCH_LIMIT)
    result: dict[str, Any] = {"service": args.service or "all",
                              "window": [start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ")],
                              "matching_lines": len(lines), "patterns": summarize_patterns(lines)}
    if len(lines) >= PATTERN_FETCH_LIMIT:
        result["note"] = "line limit reached: only the newest lines were analyzed, first_seen may be later than the real onset"
    return result


LOKI_QUERY = Tool("loki_query", "Run a LogQL log query and return the newest matching lines (compacted).",
                  LokiQueryArgs, loki_query)
LOKI_LABEL_VALUES = Tool("loki_label_values", "List values of a log label (e.g. which services have logs).",
                         LabelValuesArgs, loki_label_values)
ERROR_PATTERN_SUMMARY = Tool(
    "error_pattern_summary",
    "Group warning/error log lines into normalized patterns with count, first_seen, last_seen and an example. "
    "Best first step for log analysis.",
    ErrorPatternArgs, error_pattern_summary)
