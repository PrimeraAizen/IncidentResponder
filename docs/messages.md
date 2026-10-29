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

> **Provenance of the examples.** Every example is verbatim from the final evaluation (`gemini-3.1-flash-lite`, 27 Oct 2026), read from the state store `data/incidents.db`. `…` only marks fields that repeat an earlier value.
> - **One investigation from start to end:** s05 run 3 (notification memory leak). This is the evaluation's typical run, committed as `logs/sample/typical_run.jsonl` and `reports/sample/s05_notification_memory_leak_report.md`, so its values can be checked in the repository.
> - **A deploy scenario:** s09 run 3, for `suspect_commit`.
> - **The verify loop:** s03 run 3, for rejected verdicts and the final-decision rule.
> - **`AgentFailure`:** a run that was not scored because Gemini was overloaded.

---

## Alert

```json
{
  "alert_name": "HighMemoryUsage",
  "service": "notification",
  "severity": "warning",
  "started_at": "2026-10-27T22:07:14.632171Z",
  "summary": "notification memory grows quickly; container restarted",
  "labels": {
    "alertname": "HighMemoryUsage",
    "service": "notification"
  }
}
```

## InvestigationPlan and Task

Produced by the Orchestrator. It chose a 15-minute look-back and focused on notification and its event source, NATS, with one objective per specialist. Ids and timestamps are filled in by code.

```json
{
  "incident_id": "inc-20261027-220814-7498",
  "alert": {
    "...": "the Alert above"
  },
  "window_start": "2026-10-27T21:52:14.632171Z",
  "window_end": "2026-10-27T22:08:15.006263Z",
  "focus_services": [
    "notification",
    "nats"
  ],
  "tasks": [
    {
      "task_id": "inc-20261027-220814-7498-log",
      "agent": "log_analyst",
      "objective": "Search for error logs, panic traces, or memory-related warnings in the notification service logs leading up to the container restart.",
      "window_start": "2026-10-27T21:52:14.632171Z",
      "window_end": "2026-10-27T22:08:15.006263Z",
      "focus_services": [
        "notification",
        "nats"
      ]
    },
    {
      "task_id": "inc-20261027-220814-7498-metrics",
      "agent": "metrics_analyst",
      "objective": "Compare memory usage, goroutine counts, and NATS message consumption rates for the notification service before and during the memory growth period.",
      "window_start": "…",
      "window_end": "…",
      "focus_services": "…"
    },
    {
      "task_id": "inc-20261027-220814-7498-change",
      "agent": "change_investigator",
      "objective": "Identify any recent deployments, configuration changes, or container restart events for the notification service and NATS broker.",
      "window_start": "…",
      "window_end": "…",
      "focus_services": "…"
    }
  ]
}
```

## EvidenceBundle and Evidence

**Metrics Analyst.** `raw_ref` holds the query that produced each observation, so every claim can be re-run:

```json
{
  "agent": "metrics_analyst",
  "task_id": "inc-20261027-220814-7498-metrics",
  "status": "ok",
  "evidence": [
    {
      "evidence_id": "metrics-1",
      "source": "metrics",
      "service": "notification",
      "signal": "heap memory usage increased from 2.57MB baseline to 53.29MB during incident (z=100)",
      "observed_at": "2026-10-27T22:06:44Z",
      "strength": 1.0,
      "raw_ref": "go_memstats_heap_alloc_bytes{service=\"notification\"}"
    },
    {
      "evidence_id": "metrics-2",
      "source": "metrics",
      "service": "notification",
      "signal": "notification service experienced container restarts (restarts_5m metric)",
      "observed_at": "2026-10-27T22:07:14Z",
      "strength": 0.8,
      "raw_ref": "changes(process_start_time_seconds{service=\"notification\"}[5m])"
    }
  ],
  "notes": "The notification service shows a massive heap memory spike leading to the reported container restarts. Other services show no significant anomalies in latency, error rates, or resource usage."
}
```

**Change Investigator.** No deploy touched notification; the container restart comes from the Docker API:

```json
{
  "agent": "change_investigator",
  "task_id": "inc-20261027-220814-7498-change",
  "status": "ok",
  "evidence": [
    {
      "evidence_id": "changes-1",
      "source": "changes",
      "service": "notification",
      "signal": "notification container restarted 1x, 29 s after the alert (started_at 2026-10-27T22:07:44Z)",
      "observed_at": "2026-10-27T22:07:44Z",
      "strength": 0.9,
      "raw_ref": "docker_inspect"
    }
  ],
  "notes": "The notification service restarted shortly after the alert. No deployments were recorded for notification or nats during the window; other services are running normally with no restarts."
}
```

**Log Analyst.** An empty bundle is an answer too. A memory leak is silent in the logs, and saying so explicitly keeps the Diagnostician from guessing:

```json
{
  "agent": "log_analyst",
  "task_id": "inc-20261027-220814-7498-log",
  "status": "ok",
  "evidence": [],
  "notes": "No error, warning, or panic logs were found in the notification service during the investigation window; the service appears to be functioning normally despite the reported memory alert."
}
```

`status` is `ok`, `partial` (a tool failed, so only part of the data was seen) or `failed` (the agent could not finish; `evidence` is empty and `notes` holds the error).

**A deploy scenario (s09 run 3).** The Change Investigator puts the deploy's commit sha into `raw_ref`, which is how the Diagnostician can name a suspect commit:

```json
{
  "agent": "change_investigator",
  "task_id": "inc-20261027-223614-852c-change",
  "status": "ok",
  "evidence": [
    {
      "evidence_id": "changes-1",
      "source": "changes",
      "service": "order",
      "signal": "order v1.4.3 deployed 30 s before the alert (commit 2fb7998 by N. Abenov, 'order: drop unused orders(customer_id, created_at) index to speed up inserts'); migration dropped a database index which may be causing performance degradation on order creation queries",
      "observed_at": "2026-10-27T22:34:43Z",
      "strength": 0.9,
      "raw_ref": "2fb7998"
    }
  ],
  "notes": "The order service deployment occurred shortly before the latency alert. All other services are running normally with no container restarts or OOM kills."
}
```

### What the tools return

The specialists never see raw data: tools return summaries. These two outputs are from an earlier s01 run (Phase 1, 6 Oct 2026). The final tools return the same fields plus:
- `error_pattern_summary`: `window` and `matching_lines`, and for each pattern `first_vs_alert_s` / `last_vs_alert_s` (seconds relative to the alert, negative = before);
- `zscore_anomaly`: `promql` and `total_series`, and a `note` on series that did not exist in the baseline.

With the times relative to the alert, the LLM never does date arithmetic.

```json
{"service": "gateway", "pattern": "WARN slow upstream response upstream=payment route=/charge status=200",
 "count": 571, "first_seen": "2026-10-06T11:48:08Z", "last_seen": "2026-10-06T11:49:26Z",
 "example": "level=WARN msg=slow upstream response route=/charge status=200 latency_ms=2064 upstream=payment"}
```

```json
{"baseline": ["2026-10-06T11:51:38Z", "2026-10-06T11:56:38Z"], "incident": ["2026-10-06T11:57:08Z", "2026-10-06T11:58:50Z"],
 "series": [
   {"labels": {"service": "gateway", "target": "payment"}, "baseline_mean": 0.0975, "incident_mean": 2.9131, "ratio": 29.88, "zscore": 100.0},
   {"labels": {"service": "gateway", "target": "order"},   "baseline_mean": 0.0049, "incident_mean": 0.0053, "ratio": 1.09,  "zscore": 0.94}]}
```

## HypothesisSet and Hypothesis

```json
{
  "hypotheses": [
    {
      "hypothesis_id": "h1",
      "root_cause_service": "notification",
      "fault_type": "memory_leak",
      "explanation": "The notification service exhibits a rapid, sustained increase in heap memory usage, which triggered the HighMemoryUsage alert and eventually led to a container restart. Given the lack of recent code changes or external errors, this points to an internal memory leak within the notification service.",
      "supporting_evidence": [
        "metrics-1",
        "metrics-2",
        "changes-1"
      ],
      "suspect_commit": null,
      "confidence": 0.9
    }
  ]
}
```

`fault_type` is one of `latency, dependency_down, bad_deploy, memory_leak, goroutine_leak, resource_exhaustion, misconfiguration, slow_query, no_fault`. At most 3 hypotheses, ranked.

**s09 run 3.** A deploy shipped the fault, but the fault type is `slow_query` at postgres, not `bad_deploy`. Taxonomy rule: a migration that slows database queries is a slow query, even when a service's deploy shipped it. The commit is still named:

```json
{
  "hypotheses": [
    {
      "hypothesis_id": "h1",
      "root_cause_service": "postgres",
      "fault_type": "slow_query",
      "explanation": "The deployment of commit 2fb7998 dropped a database index, which caused the 'recent_orders' query to slow down significantly. This increased latency in the order service's /orders endpoint, eventually leading to the observed high latency and subsequent service restart.",
      "supporting_evidence": [
        "changes-1",
        "metrics-1",
        "metrics-3"
      ],
      "suspect_commit": "2fb7998",
      "confidence": 0.9
    }
  ]
}
```

## Verdict

**Confirmed (s05 run 3).** The Verifier designed its own checks, without seeing the specialists' evidence. Two things in the queries come from code and prompt rules:
- **`offset 4m` is computed by code** (`baseline_offset_minutes`), so the comparison with "before the incident" never reaches into the incident itself.
- **The peak is compared, not the current value** (`max_over_time`). After the OOM kill, the new process has little memory again, so a restart erases its own evidence.

```json
{
  "hypothesis_id": "h1",
  "status": "confirmed",
  "checks": [
    {
      "evidence_id": "verify-1",
      "source": "verification",
      "service": "notification",
      "signal": "heap memory usage increased by ~36x compared to 4 minutes ago",
      "observed_at": "2026-10-27T22:08:44Z",
      "strength": 1.0,
      "raw_ref": "max_over_time(go_memstats_heap_alloc_bytes{service=\"notification\"}[4m]) / max_over_time(go_memstats_heap_alloc_bytes{service=\"notification\"}[4m] offset 4m)"
    },
    {
      "evidence_id": "verify-2",
      "source": "verification",
      "service": "notification",
      "signal": "2 process restarts detected in the last 10 minutes",
      "observed_at": "2026-10-27T22:08:44Z",
      "strength": 0.9,
      "raw_ref": "changes(process_start_time_seconds{service=\"notification\"}[10m])"
    }
  ],
  "reasoning": "The notification service shows a massive, rapid increase in heap memory usage and multiple recent process restarts, confirming the memory leak hypothesis. No error logs were found, consistent with an internal memory management issue rather than external failures."
}
```

**Rejected, twice (s03 run 3: Redis stopped).** The Diagnostician's first hypothesis was right: redis / dependency_down, confidence 1.0.

1. **The first verdict was a false rejection.** Its check `verify-2` divides the cumulative counter `cache_requests_total` by its own value 4 minutes earlier. A counter that has been growing since the process started stays close to its old value whatever happened in those 4 minutes; the check needed `rate()`. The Verifier read the ratio (~1.3) as "cache hits unchanged":

```json
{
  "hypothesis_id": "h1",
  "status": "rejected",
  "checks": [
    {
      "evidence_id": "verify-1",
      "source": "verification",
      "service": "redis",
      "signal": "up{service='redis'} returned no series, indicating the service is down",
      "observed_at": "2026-10-27T21:54:22Z",
      "strength": 0.8,
      "raw_ref": "up{service='redis'}"
    },
    {
      "evidence_id": "verify-2",
      "source": "verification",
      "service": "inventory",
      "signal": "cache_requests_total ratio is ~1.3 (hit) and ~1.0 (miss), showing no significant drop in cache hits",
      "observed_at": "2026-10-27T21:54:22Z",
      "strength": 0.9,
      "raw_ref": "cache_requests_total{service='inventory'} / (cache_requests_total{service='inventory'} offset 4m)"
    }
  ],
  "reasoning": "While Redis is indeed down, the cache metrics show that the inventory service is still successfully hitting the cache at a rate comparable to before the incident. The latency increase is likely due to the database performance degradation (2x slower) rather than a fallback from a missing cache."
}
```

2. **The Diagnostician then proposed the Verifier's idea,** postgres / slow_query (confidence 0.9). The second verdict rejected it **correctly**: the query time did not change, and the health check shows Redis failing:

```json
{
  "hypothesis_id": "h1",
  "status": "rejected",
  "checks": [
    {
      "evidence_id": "verify-1",
      "source": "verification",
      "service": "inventory",
      "signal": "db_query_duration_seconds ratio is 1.0 (no change)",
      "observed_at": "2026-10-27T21:54:41Z",
      "strength": 1.0,
      "raw_ref": "histogram_quantile(0.95, sum by (le, query) (rate(db_query_duration_seconds_bucket{service=\"inventory\"}[1m]))) / histogram_quantile(0.95, sum by (le, query) (rate(db_query_duration_seconds_bucket{service=\"inventory\"}[1m] offset 4m)))"
    },
    {
      "evidence_id": "verify-2",
      "source": "verification",
      "service": "inventory",
      "signal": "health_check status 503, redis check failed (EOF)",
      "observed_at": "2026-10-27T21:54:41Z",
      "strength": 1.0,
      "raw_ref": "health_check"
    }
  ],
  "reasoning": "The database query performance has not degraded (ratio is 1.0). The inventory service is reporting a 503 status due to a Redis connection failure (EOF), which is the actual cause of the latency spike."
}
```

3. **The final-decision rule settled it.** When the Verifier rejects every hypothesis it tested, its checks did not settle the question. The report then uses the Diagnostician's most confident hypothesis, marked `partial`, with confidence × 0.5. The run was scored correct:

```json
{
  "status": "partial",
  "root_cause_service": "redis",
  "fault_type": "dependency_down",
  "confidence": 0.5,
  "limitations": [
    "the Verifier rejected every hypothesis it tested; reporting the Diagnostician's most confident one. Last objection: The database query performance has not degraded (ratio is 1.0). The inventory service is reporting a 503 status due to a Redis connection failure (EOF), which i"
  ]
}
```

This false rejection is the Verifier's typical mistake; see "Known limitations" in [design-decisions.md](design-decisions.md).

## IncidentReport

The root cause, fault type, confidence and status are filled in by code (`final_decision`). Here that is 0.9 from the Diagnostician + 0.1 for a confirmed verdict. The summary, timeline and actions are written by the Reporter LLM. The Markdown version ([`reports/sample/`](../reports/sample/s05_notification_memory_leak_report.md)) is rendered from this object:

```json
{
  "incident_id": "inc-20261027-220814-7498",
  "status": "complete",
  "root_cause_service": "notification",
  "fault_type": "memory_leak",
  "confidence": 1.0,
  "suspect_commit": null,
  "summary": "Users experienced service instability in the notification service due to a rapid, sustained increase in heap memory usage. This memory leak triggered the HighMemoryUsage alert and caused multiple container restarts. The team has high confidence in this diagnosis as the memory growth was observed directly in metrics.",
  "timeline": [
    "22:06:44 - Heap memory usage increased from 2.57MB to 53.29MB",
    "22:07:14 - HighMemoryUsage alert triggered",
    "22:07:44 - Notification container restarted",
    "22:08:44 - Verification confirmed 36x memory increase and multiple process restarts"
  ],
  "evidence_refs": [
    "metrics-1",
    "metrics-2",
    "changes-1",
    "verify-1",
    "verify-2"
  ],
  "recommended_actions": [
    "Perform a heap dump analysis to identify the specific objects causing the memory leak.",
    "Implement memory usage limits and proactive alerts for heap growth thresholds.",
    "Add automated integration tests to monitor memory consumption during service operation.",
    "Review recent dependency updates for potential memory management regressions."
  ],
  "limitations": []
}
```

## AgentFailure

From s11 run 1 of the evaluation, while Gemini was overloaded:
- Two specialists got no answer after 3 attempts.
- The time limit then stopped the Diagnostician.
- The graph still finished, with a `failed` report that names the missing data.

Because the failures came from the provider, this run was not scored and was run again (design decision 61).

```json
[
  {
    "agent": "log_analyst",
    "error": "LLM call failed (gemini-3.1-flash-lite): Error code: 503 - [{'error': {'code': 503, 'message': 'This model is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later.', 'status': 'UNAVAILABLE'}}]",
    "attempts": 3
  },
  {
    "agent": "metrics_analyst",
    "error": "LLM call failed (gemini-3.1-flash-lite): Error code: 503 - [{'error': {'code': 503, 'message': 'This model is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later.', 'status': 'UNAVAILABLE'}}]",
    "attempts": 3
  },
  {
    "agent": "diagnostician",
    "error": "run time limit reached (RUN_TIMEOUT_S=300)",
    "attempts": 1
  }
]
```

## Call log line (not a message, but the same JSON discipline)

One line per LLM call and per tool call in `logs/run_<run_id>.jsonl`. Tool calls also log their arguments, so every query an agent ran can be audited. From the committed typical run:

```json
{"ts": "2026-10-27T22:08:39.908+00:00", "run_id": "20261027T220814Z-c36df0", "incident_id": "inc-20261027-220814-7498", "agent": "diagnostician", "call_type": "llm", "name": "gemini-3.1-flash-lite", "latency_ms": 2648, "status": "ok", "attempt": 1, "tokens_in": 1978, "tokens_out": 10, "error": null, "args": null}
{"ts": "2026-10-27T22:08:45.969+00:00", "run_id": "20261027T220814Z-c36df0", "incident_id": "inc-20261027-220814-7498", "agent": "verifier", "call_type": "tool", "name": "prom_query", "latency_ms": 7, "status": "ok", "attempt": 1, "tokens_in": null, "tokens_out": null, "error": null, "args": "promql=max_over_time(go_memstats_heap_alloc_bytes{service=\"notification\"}[4m]) / max_over_time(go_memstats_heap_alloc_bytes{service=\"notification\"}[4m] offset 4m)"}
```
