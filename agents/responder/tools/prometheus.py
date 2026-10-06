"""Prometheus tools (Metrics Analyst, Verifier).

Raw time series are never sent to the LLM. Range queries are reduced to a
per-series summary (min/max/mean/last + the biggest level shift), and
zscore_anomaly compares an incident window with the baseline before it in pure
Python. The LLM gets numbers it can reason about, not hundreds of points.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from statistics import fmean, pstdev
from typing import Any

from pydantic import BaseModel, Field

from responder.schemas import UTCDateTime
from responder.tools.base import NoArgs, Tool, ToolContext, ToolError

MAX_SERIES = 10
_DROP_LABELS = {"__name__", "instance", "job"}  # `service` already identifies the target


# ------------------------------------------------------------------- HTTP helper
def _prom(ctx: ToolContext, path: str, params: dict[str, Any]) -> Any:
    resp = ctx.http.get(f"{ctx.settings.prom_url}{path}", params=params)
    if resp.status_code >= 500:
        resp.raise_for_status()  # retryable
    body = resp.json()
    if body.get("status") != "success":
        raise ToolError(f"prometheus rejected the query: {body.get('error', resp.text[:200])}")
    return body["data"]


def _labels(metric: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in metric.items() if k not in _DROP_LABELS}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _values(raw: list[list[Any]]) -> list[tuple[float, float]]:
    """[[ts, "1.2"], ...] -> [(ts, 1.2), ...] without NaN/Inf (e.g. quantiles with no traffic)."""
    out = []
    for ts, val in raw:
        v = float(val)
        if math.isfinite(v):
            out.append((float(ts), v))
    return out


def _range(ctx: ToolContext, promql: str, start: datetime, end: datetime, step: str) -> list[dict]:
    data = _prom(ctx, "/api/v1/query_range",
                 {"query": promql, "start": start.timestamp(), "end": end.timestamp(), "step": step})
    return data["result"]


# --------------------------------------------------------------- pure functions
def change_point(values: list[float], min_segment: int = 3) -> tuple[int, float, float] | None:
    """Index that splits the series into two parts with the most different means.

    Returns (index, mean_before, mean_after) or None if the series is too short.
    Simple and explainable: try every split, keep the largest |mean difference|.
    """
    n = len(values)
    if n < 2 * min_segment:
        return None
    best: tuple[int, float, float] | None = None
    best_gap = -1.0
    for k in range(min_segment, n - min_segment + 1):
        before, after = fmean(values[:k]), fmean(values[k:])
        if abs(after - before) > best_gap:
            best_gap, best = abs(after - before), (k, before, after)
    return best


def zscore(baseline: list[float], incident: list[float]) -> dict[str, float | None]:
    """How unusual the incident mean is compared with the baseline.

    The standard deviation is floored at 10% of the baseline mean: very stable
    baselines (std ~ 0) would otherwise turn tiny changes into huge z-scores.
    """
    inc_mean = fmean(incident)
    if not baseline:
        return {"baseline_mean": None, "baseline_std": None, "incident_mean": round(inc_mean, 4),
                "ratio": None, "zscore": None}
    base_mean, base_std = fmean(baseline), pstdev(baseline)
    std = max(base_std, 0.1 * abs(base_mean), 1e-9)
    z = max(-100.0, min(100.0, (inc_mean - base_mean) / std))
    ratio = inc_mean / base_mean if base_mean else None
    return {"baseline_mean": round(base_mean, 4), "baseline_std": round(base_std, 4),
            "incident_mean": round(inc_mean, 4), "ratio": round(ratio, 2) if ratio is not None else None,
            "zscore": round(z, 2)}


# -------------------------------------------------------------------- tools
class PromQueryArgs(BaseModel):
    promql: str = Field(description="PromQL expression")
    at: UTCDateTime | None = Field(None, description="Evaluation time (ISO 8601). Default: end of the window.")


def prom_query(args: PromQueryArgs, ctx: ToolContext) -> dict:
    at = args.at or ctx.window_end
    data = _prom(ctx, "/api/v1/query", {"query": args.promql, "time": at.timestamp()})
    if data["resultType"] == "scalar":
        return {"promql": args.promql, "at": _iso(at.timestamp()), "value": float(data["result"][1])}
    series = [{"labels": _labels(s["metric"]), "value": round(float(s["value"][1]), 4)} for s in data["result"]]
    series.sort(key=lambda s: -s["value"] if math.isfinite(s["value"]) else 0)
    return {"promql": args.promql, "at": _iso(at.timestamp()), "total_series": len(series),
            "series": series[:MAX_SERIES * 2]}


class PromRangeArgs(BaseModel):
    promql: str = Field(description="PromQL expression")
    start: UTCDateTime | None = Field(None, description="Default: start of the investigation window")
    end: UTCDateTime | None = Field(None, description="Default: end of the investigation window")
    step: str = Field("15s", description="Resolution, e.g. 15s or 1m")


def prom_query_range(args: PromRangeArgs, ctx: ToolContext) -> dict:
    start, end = ctx.window(args.start, args.end)
    out = []
    for s in _range(ctx, args.promql, start, end, args.step):
        points = _values(s["values"])
        if not points:
            continue
        vals = [v for _, v in points]
        summary: dict[str, Any] = {"labels": _labels(s["metric"]), "min": round(min(vals), 4),
                                   "max": round(max(vals), 4), "mean": round(fmean(vals), 4),
                                   "last": round(vals[-1], 4)}
        cp = change_point(vals)
        if cp:
            k, before, after = cp
            summary["shift"] = {"at": _iso(points[k][0]), "mean_before": round(before, 4),
                                "mean_after": round(after, 4)}
        out.append(summary)
    # Most changed series first.
    out.sort(key=lambda s: -abs(s.get("shift", {}).get("mean_after", 0) - s.get("shift", {}).get("mean_before", 0)))
    return {"promql": args.promql, "window": [_iso(start.timestamp()), _iso(end.timestamp())],
            "total_series": len(out), "series": out[:MAX_SERIES]}


class ZScoreArgs(BaseModel):
    promql: str = Field(description="PromQL expression; every returned series is scored")
    baseline_start: UTCDateTime | None = Field(None, description="Default: start of the investigation window")
    baseline_end: UTCDateTime | None = Field(None, description="Default: 60 s before the alert")
    incident_start: UTCDateTime | None = Field(None, description="Default: 30 s before the alert")
    incident_end: UTCDateTime | None = Field(None, description="Default: end of the investigation window")


def zscore_anomaly(args: ZScoreArgs, ctx: ToolContext) -> dict:
    (b_start, b_end), (i_start, i_end) = ctx.baseline_and_incident()
    b_start, b_end = args.baseline_start or b_start, args.baseline_end or b_end
    i_start, i_end = args.incident_start or i_start, args.incident_end or i_end
    if not (b_start < b_end <= i_start < i_end):
        raise ToolError("windows must satisfy baseline_start < baseline_end <= incident_start < incident_end")

    scored = []
    for s in _range(ctx, args.promql, b_start, i_end, "15s"):
        points = _values(s["values"])
        baseline = [v for ts, v in points if ts < b_end.timestamp()]
        incident = [v for ts, v in points if ts >= i_start.timestamp()]
        if not incident:
            continue
        result = {"labels": _labels(s["metric"]), **zscore(baseline, incident)}
        if not baseline:
            result["note"] = "series did not exist in the baseline window (new)"
        scored.append(result)

    def rank(r: dict) -> float:  # new non-zero series are as interesting as a big z-score
        return abs(r["zscore"]) if r["zscore"] is not None else (50.0 if r["incident_mean"] else 0.0)

    scored.sort(key=rank, reverse=True)
    return {"promql": args.promql,
            "baseline": [_iso(b_start.timestamp()), _iso(b_end.timestamp())],
            "incident": [_iso(i_start.timestamp()), _iso(i_end.timestamp())],
            "total_series": len(scored), "series": scored[:MAX_SERIES]}


def targets_health(args: NoArgs, ctx: ToolContext) -> dict:
    data = _prom(ctx, "/api/v1/targets", {"state": "active"})
    targets = [{"service": t["labels"].get("service", t["labels"].get("job")), "health": t["health"],
                "last_error": t.get("lastError") or None, "last_scrape": t.get("lastScrape", "")[:19]}
               for t in data["activeTargets"]]
    return {"targets": sorted(targets, key=lambda t: t["service"])}


PROM_QUERY = Tool("prom_query", "Evaluate a PromQL expression at one instant; returns the value per series.",
                  PromQueryArgs, prom_query)
PROM_QUERY_RANGE = Tool(
    "prom_query_range",
    "Run a PromQL range query and return per-series min/max/mean/last plus the biggest level shift "
    "(when it happened, mean before/after). Raw points are not returned.",
    PromRangeArgs, prom_query_range)
ZSCORE_ANOMALY = Tool(
    "zscore_anomaly",
    "Compare each series of a PromQL expression in the incident window against the baseline window "
    "before it. Returns baseline_mean, incident_mean, ratio and z-score per series, most anomalous first.",
    ZScoreArgs, zscore_anomaly)
TARGETS_HEALTH = Tool("targets_health", "Scrape health of every service known to Prometheus (up/down + last error).",
                      NoArgs, targets_health)
