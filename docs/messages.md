# Message schemas

All agents exchange **structured messages**: Pydantic v2 models defined in [`agents/responder/schemas.py`](../agents/responder/schemas.py) and serialized as JSON. A message is validated when it is produced, so a malformed LLM answer never reaches the next agent. The runtime asks the model to repair it once, and otherwise returns `AgentFailure`.

| Message | Producer → Consumer | Purpose |
|---|---|---|
| `Alert` | harness / Alertmanager → Orchestrator | the symptom (never the cause) |
| `InvestigationPlan` (with `Task`s) | Orchestrator → specialists, Diagnostician, Verifier | time window, focus services, one task per specialist |
| `EvidenceBundle` (with `Evidence` items) | Log / Metrics / Change → Diagnostician | observations with strength and the query that produced them |
| `HypothesisSet` (with `Hypothesis` items) | Diagnostician → Verifier, Reporter | ranked root-cause candidates citing evidence ids |
| `Verdict` | Verifier → Diagnostician (loop) or Reporter | confirmed / rejected / inconclusive + the checks performed |
| `IncidentReport` | Reporter → file (`reports/<id>.md/.json`) | final result |
| `AgentFailure` | any agent → graph state | typed failure instead of an exception (graceful degradation) |

**Code-owned fields.** Some fields are filled in by code, never by the LLM, so they are always correct and unique:
- `EvidenceBundle.agent` and `task_id`;
- `Evidence.evidence_id` (`logs-1`, `metrics-2`, …) and `source`;
- `Hypothesis.hypothesis_id` (`h1`…`h3`).

References to evidence ids that do not exist are dropped.

> **Provenance of the examples.**
> - `EvidenceBundle` and `HypothesisSet` are verbatim from a real Phase 1 run on scenario s01 with `gemini-3.5-flash`: `logs/sample/phase1_s01_gemini_result.json`, call log `logs/sample/phase1_s01_gemini.jsonl`. The fault was injected at 12:51:36 UTC.
> - `Alert`, `InvestigationPlan` and `Task` come from an earlier s01 run; the structure is identical.
> - `Verdict` and `IncidentReport` are illustrative until Phase 3.

---

## Alert

```json
{
  "alert_name": "HighLatency",
  "service": "gateway",
  "severity": "critical",
  "started_at": "2026-10-06T11:48:36.404182Z",
  "summary": "p95 latency of POST /api/orders > 1s",
  "labels": {"alertname": "HighLatency", "service": "gateway"}
}
```

## InvestigationPlan and Task

```json
{
  "incident_id": "inc-20261006-115026-333a",
  "alert": { "...": "the Alert above" },
  "window_start": "2026-10-06T11:33:36.404182Z",
  "window_end": "2026-10-06T11:50:26.633991Z",
  "focus_services": ["gateway", "inventory", "payment", "order", "redis", "postgres", "nats"],
  "tasks": [
    {
      "task_id": "inc-20261006-115026-333a-logs",
      "agent": "log_analyst",
      "objective": "Find warning/error log patterns, when they started and which services emit them, around the HighLatency alert on gateway.",
      "window_start": "2026-10-06T11:33:36.404182Z",
      "window_end": "2026-10-06T11:50:26.633991Z",
      "focus_services": ["gateway", "inventory", "payment", "order", "redis", "postgres", "nats"]
    },
    {
      "task_id": "inc-20261006-115026-333a-metrics",
      "agent": "metrics_analyst",
      "objective": "Find anomalies in latency, error rates, calls between services, DB pool and resource metrics for gateway, inventory, payment, order, redis, postgres, nats, compared with the baseline before the HighLatency alert.",
      "window_start": "2026-10-06T11:33:36.404182Z",
      "window_end": "2026-10-06T11:50:26.633991Z",
      "focus_services": ["gateway", "inventory", "payment", "order", "redis", "postgres", "nats"]
    }
  ]
}
```

## EvidenceBundle and Evidence

Log Analyst:

```json
{
  "agent": "log_analyst",
  "task_id": "inc-20261006-130030-cd27-logs",
  "status": "ok",
  "evidence": [
    {
      "evidence_id": "logs-1",
      "source": "logs",
      "service": "gateway",
      "signal": "4015 WARN 'slow upstream response' lines from gateway with upstream=payment, route=/charge, latency_ms ≈ 2062, first at 12:51:38Z",
      "observed_at": "2026-10-06T12:51:38Z",
      "strength": 0.95,
      "raw_ref": "error_pattern_summary"
    }
  ],
  "notes": "The gateway service started logging slow upstream responses from the payment service (/charge route) with latency around 2 seconds, starting at 12:51:38Z, which directly correlates with the HighLatency alert."
}
```

Metrics Analyst. Note that it also reports the signals that stayed **normal** (with low strength). "Payment's own `/charge` latency is unchanged" is what tells the Diagnostician that the delay is on the gateway→payment path.

```json
{
  "agent": "metrics_analyst",
  "task_id": "inc-20261006-130030-cd27-metrics",
  "status": "ok",
  "evidence": [
    {"evidence_id": "metrics-1", "source": "metrics", "service": "gateway",
     "signal": "p95 latency of POST /api/orders increased to 2.86s (baseline 0.10s, z=100.0)",
     "observed_at": "2026-10-06T12:51:36Z", "strength": 1.0,
     "raw_ref": "histogram_quantile(0.95, sum by (le, service, route) (rate(http_request_duration_seconds_bucket[1m])))"},
    {"evidence_id": "metrics-2", "source": "metrics", "service": "order",
     "signal": "p95 latency of /orders route remained normal at 0.0048s (z=0.0)",
     "observed_at": null, "strength": 0.1, "raw_ref": "histogram_quantile(0.95, …)"},
    {"evidence_id": "metrics-3", "source": "metrics", "service": "payment",
     "signal": "p95 latency of /charge route remained normal at 0.0975s (z=0.0)",
     "observed_at": null, "strength": 0.1, "raw_ref": "histogram_quantile(0.95, …)"}
  ],
  "notes": "The gateway service shows a significant latency spike on POST /api/orders starting around 12:51:36 UTC, while downstream HTTP routes on order and payment services remain normal."
}
```

`status` is `ok`, `partial` (a tool failed, so only part of the data was seen) or `failed` (the agent could not finish; `evidence` is empty and `notes` holds the error).

For reference, this is raw `error_pattern_summary` tool output from an earlier s01 run. It is the kind of data the Log Analyst turns into the evidence above:

```json
{"service": "gateway", "pattern": "WARN slow upstream response upstream=payment route=/charge status=200",
 "count": 571, "first_seen": "2026-10-06T11:48:08Z", "last_seen": "2026-10-06T11:49:26Z",
 "example": "level=WARN msg=slow upstream response route=/charge status=200 latency_ms=2064 upstream=payment"}
```

And this is the real `zscore_anomaly` output for p95 latency of calls between services, from a second s01 run on a fresh stack (fault injected at 11:57:08):

```json
{"baseline": ["2026-10-06T11:51:38Z", "2026-10-06T11:56:38Z"], "incident": ["2026-10-06T11:57:08Z", "2026-10-06T11:58:50Z"],
 "series": [
   {"labels": {"service": "gateway", "target": "payment"}, "baseline_mean": 0.0975, "incident_mean": 2.9131, "ratio": 29.88, "zscore": 100.0},
   {"labels": {"service": "gateway", "target": "order"},   "baseline_mean": 0.0049, "incident_mean": 0.0053, "ratio": 1.09,  "zscore": 0.94}]}
```

On the same query by route, payment's own `POST /charge` was unchanged (ratio 1.0). Only the gateway→payment path got slower.

## HypothesisSet and Hypothesis

```json
{
  "hypotheses": [
    {
      "hypothesis_id": "h1",
      "root_cause_service": "payment",
      "fault_type": "latency",
      "explanation": "The gateway is experiencing high latency because calls to the payment service (/charge) are taking over 2 seconds, as captured in the gateway logs, pointing to a latency issue in the payment service.",
      "supporting_evidence": ["logs-1", "metrics-1"],
      "suspect_commit": null,
      "confidence": 0.8
    }
  ]
}
```

`fault_type` is one of `latency, dependency_down, bad_deploy, memory_leak, goroutine_leak, resource_exhaustion, misconfiguration, slow_query, no_fault`. At most 3 hypotheses, ranked.

## Verdict (illustrative, Phase 3)

```json
{
  "hypothesis_id": "h1",
  "status": "confirmed",
  "checks": [
    {"evidence_id": "verify-1", "source": "verification", "service": "gateway",
     "signal": "p95 of gateway→payment calls still 2.05 s; gateway→order p95 normal (0.01 s)",
     "observed_at": "2026-10-06T11:51:02Z", "strength": 0.9,
     "raw_ref": "histogram_quantile(0.95, sum by (le, target) (rate(upstream_request_duration_seconds_bucket{service=\"gateway\"}[1m])))"}
  ],
  "reasoning": "Only calls to payment are slow; payment answers its own health check quickly, so the delay is in the payment path."
}
```

## IncidentReport (illustrative, Phase 3)

```json
{
  "incident_id": "inc-20261006-115026-333a",
  "status": "complete",
  "root_cause_service": "payment",
  "fault_type": "latency",
  "confidence": 0.85,
  "suspect_commit": null,
  "summary": "Charges to the payment provider became ~2 s slower at 11:48, which made POST /api/orders on the gateway exceed its latency SLO.",
  "timeline": ["11:48:08 first slow payment responses", "11:48:36 HighLatency alert on gateway", "11:51 hypothesis confirmed"],
  "evidence_refs": ["logs-1", "metrics-1", "verify-1"],
  "recommended_actions": ["Check the payment provider status / network path", "Lower the gateway payment timeout and add a circuit breaker"],
  "limitations": []
}
```

## AgentFailure

From the degradation unit test (`test_failed_specialist_degrades_gracefully`), where the provider is down for one agent:

```json
{"agent": "metrics_analyst", "error": "LLM call failed (fake): simulated provider outage", "attempts": 3}
```

## Call log line (not a message, but the same JSON discipline)

One line per LLM call and per tool call in `logs/run_<run_id>.jsonl`:

```json
{"ts": "2026-10-06T13:00:32.055+00:00", "run_id": "20261006T130030Z-927491", "incident_id": "inc-20261006-130030-cd27",
 "agent": "metrics_analyst", "call_type": "llm", "name": "gemini-3.5-flash", "latency_ms": 1853, "status": "ok",
 "attempt": 1, "tokens_in": 2014, "tokens_out": 10, "error": null}
```
