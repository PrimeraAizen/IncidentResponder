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

> **Provenance of the examples.** All examples are verbatim from real runs on scenario s01 (payment +2 s latency).
> - `Alert`, the log/metrics `EvidenceBundle`s and the Phase 1 `HypothesisSet`: a Phase 1 run with `gemini-3.5-flash` (`logs/sample/phase1_s01_gemini*`).
> - The Orchestrator's `InvestigationPlan`, the Change Investigator's `EvidenceBundle`, the `Verdict` and the `IncidentReport`: a Phase 4 evaluation run with all 7 agents and `gemini-3.1-flash-lite` (17 Oct 2026).
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

Produced by the Orchestrator. It chose a 10-minute look-back and wrote one objective per specialist; ids and timestamps are filled in by code.

```json
{
  "incident_id": "inc-20261017-103122-f332",
  "alert": {
    "...": "the Alert"
  },
  "window_start": "2026-10-17T10:20:21.938198Z",
  "window_end": "2026-10-17T10:31:22.574657Z",
  "focus_services": [
    "gateway",
    "inventory",
    "payment",
    "order",
    "postgres",
    "redis",
    "nats"
  ],
  "tasks": [
    {
      "task_id": "inc-20261017-103122-f332-log",
      "agent": "log_analyst",
      "objective": "Search for error logs, timeouts, or slow query warnings in gateway, inventory, payment, and order services starting from 10:20:00Z.",
      "window_start": "…",
      "window_end": "…",
      "focus_services": "…"
    },
    {
      "task_id": "inc-20261017-103122-f332-metrics",
      "agent": "metrics_analyst",
      "objective": "Compare p95 latency, request rates, and resource utilization (DB connection pools, Redis latency, NATS queue depth) across all services between 10:20:00Z and 10:31:22Z.",
      "window_start": "…",
      "window_end": "…",
      "focus_services": "…"
    },
    {
      "task_id": "inc-20261017-103122-f332-change",
      "agent": "change_investigator",
      "objective": "Identify any deployments, configuration changes, or container restarts/OOM kills for gateway, inventory, payment, and order services within the last 10 minutes.",
      "window_start": "…",
      "window_end": "…",
      "focus_services": "…"
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

Change Investigator (Phase 4 run). It found the two harmless noise deploys and rated their relevance. The Diagnostician correctly did not blame them:

```json
{
  "agent": "change_investigator",
  "task_id": "inc-20261017-103122-f332-change",
  "status": "ok",
  "evidence": [
    {
      "evidence_id": "changes-1",
      "source": "changes",
      "service": "inventory",
      "signal": "inventory v3.0.2 deployed 151 s before the alert (commit ee96072 by A. Seitkali, 'inventory: cache the product list for 15s instead of 10s'); diff increases redis cache TTL from 10s to 15s",
      "observed_at": "2026-10-17T10:27:50Z",
      "strength": 0.6,
      "raw_ref": "ee96072"
    },
    {
      "evidence_id": "changes-2",
      "source": "changes",
      "service": "notification",
      "signal": "notification v1.2.1 deployed 152 s before the alert (commit 487c882 by D. Omarova, 'notification: friendlier order confirmation text')",
      "observed_at": "2026-10-17T10:27:49Z",
      "strength": 0.1,
      "raw_ref": "487c882"
    }
  ],
  "notes": "All containers are running with 0 restarts. The inventory cache TTL change is the only configuration modification within the window."
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

## Verdict

The Verifier designed two checks of its own, a metric and a log query, without seeing the specialists' evidence:

```json
{
  "hypothesis_id": "h1",
  "status": "confirmed",
  "checks": [
    {
      "evidence_id": "verify-1",
      "source": "verification",
      "service": "gateway",
      "signal": "p95 upstream latency to payment is 2.95s",
      "observed_at": "2026-10-17T10:32:05Z",
      "strength": 1.0,
      "raw_ref": "histogram_quantile(0.95, sum by (le, target) (rate(upstream_request_duration_seconds_bucket{service=\"gateway\"}[1m])))"
    },
    {
      "evidence_id": "verify-2",
      "source": "verification",
      "service": "gateway",
      "signal": "logs show consistent ~2s latency for payment upstream calls",
      "observed_at": "2026-10-17T10:32:05Z",
      "strength": 1.0,
      "raw_ref": "{service=\"gateway\"} | json | upstream=\"payment\" | line_format \"{{.latency_ms}}\""
    }
  ],
  "reasoning": "Metrics confirm that the gateway's p95 latency to the payment service is significantly elevated (2.95s), while other upstream dependencies remain performant. Logs corroborate this by showing consistent ~2s response times for payment-related requests, confirming the payment service as the source of the latency."
}
```

## IncidentReport

Root cause, fault type, confidence (0.9 from the Diagnostician + 0.1 for a confirmed verdict) and status are filled in by code (`final_decision`); summary, timeline and actions are written by the Reporter LLM. The Markdown version is rendered from this object:

```json
{
  "incident_id": "inc-20261017-103122-f332",
  "status": "complete",
  "root_cause_service": "payment",
  "fault_type": "latency",
  "confidence": 1.0,
  "suspect_commit": null,
  "summary": "Users experienced high latency on the POST /api/orders endpoint due to slow response times from the payment service. The investigation confirmed that the gateway service is experiencing a p95 latency of approximately 2.95s when communicating with the payment upstream. The team has high confidence in this diagnosis as metrics and logs consistently point to the payment service as the bottleneck.",
  "timeline": [
    "10:22:10 - First 'upstream request failed' errors in gateway logs",
    "10:23:38 - First 'slow upstream response' warnings in gateway logs",
    "10:29:51 - Metrics confirm elevated p95 latency for gateway-to-payment calls",
    "10:30:21 - HighLatency alert triggered for /api/orders",
    "10:32:05 - Verification confirms 2.95s p95 latency to payment service"
  ],
  "evidence_refs": [
    "logs-1",
    "logs-2",
    "metrics-1",
    "metrics-2",
    "verify-1",
    "verify-2"
  ],
  "recommended_actions": [
    "Investigate and resolve performance degradation in the payment service.",
    "Implement circuit breakers in the gateway to prevent cascading failures from slow upstream services.",
    "Set up specific latency alerts for the payment service to detect degradation before it impacts the gateway.",
    "Review and optimize timeout configurations for gateway-to-payment requests."
  ],
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
