# Test scenarios

Each scenario is a YAML file in [`scenarios/`](../scenarios). It contains:
- the **fault** (setup steps run by the chaos CLI);
- the **alert** the agents receive: the *symptom*, never the cause;
- the **expected root cause**: ground truth, used only for scoring;
- a **symptom check**: a PromQL condition the harness evaluates to prove the fault really happened.

## Scenario table

| # | Fault (how it is injected) | Alert (what the agents get) | Expected root cause | Symptom check (harness only) | Status |
|---|---|---|---|---|---|
| s01 | Toxiproxy +2 s latency gateway→payment | gateway `HighLatency`: "p95 latency of POST /api/orders > 1s" | **payment / latency** (must not blame gateway) | gateway `/api/orders` p95 > 1 s | ✅ |
| s02 | `docker stop postgres` | gateway `HighErrorRate`: "5xx ratio > 5%" | **postgres / dependency_down** | gateway 5xx ratio > 0.2 | ✅ |
| s03 | `docker stop redis` | inventory `HighLatency`: "p95 of GET /products far above normal" | **redis / dependency_down** | inventory `/products` p95 > 50 ms **and** cache errors > 0.5/s | ✅ |
| s04 | deploy-sim: order patch adds a nil-pointer bug (loyalty promotions) | gateway `HighErrorRate` | **order / bad_deploy** + the deploy's commit | gateway 5xx ratio > 0.1 | ✅ |
| s05 | `/admin/chaos leak_memory` on notification (2 MiB/s, 128 MiB limit → OOM kill + restart) | notification `HighMemoryUsage`: "memory grows quickly; container restarted" | **notification / memory_leak** | peak heap > 50 MB **and** ≥ 1 restart | ✅ |
| s06 | deploy-sim: order `config.yaml` sets DB pool `max_conns: 2`, during an order rush (40 rps, 70% orders) | gateway `HighLatency`: "p95 > 1s, some requests time out" | **order / resource_exhaustion** + the config commit | gateway `/api/orders` p95 > 1 s **and** order pool wait p95 > 0.2 s | ✅ |
| s07 | `docker stop nats` | order `EventPublishErrors` | **nats / dependency_down** (orders still succeed) | failed publishes > 0.5/s **and** gateway 5xx ratio < 0.05 | ✅ |
| s08 | deploy-sim: gateway `config.yaml` points payment at a wrong port | gateway `HighErrorRate` (502) | **gateway / misconfiguration** + the config commit (must not blame payment) | gateway 5xx ratio > 0.1 | ✅ |
| s09 | deploy-sim: an order migration drops the index of the fraud check; 3M orders, busy period (30 rps, 70% orders) | order `HighLatency` | **postgres / slow_query** (accepted: order) + the migration's commit | order `recent_orders` query p95 > 30 ms **and** order `/orders` p95 > 0.2 s | ✅ |
| s10 | `/admin/chaos leak_goroutines` on inventory (200/s; each wakes every 20 ms; inventory limited to 1 CPU) | gateway `HighLatency` on `GET /api/products` | **inventory / goroutine_leak** | inventory goroutines > 5000 **and** gateway `/api/products` p95 > 50 ms | ✅ |
| s11 | Toxiproxy +5 s on gateway→payment: every attempt hits the gateway's 3 s timeout, and 3 retries make a retry storm | gateway `HighErrorRate` + "outgoing request rate far above normal" | **payment / latency** (must not blame gateway) | gateway 5xx ratio > 0.1 **and** > 2 payment calls per order | ✅ |
| s12 | load spike ×5 (+40 rps), no fault | gateway `HighLatency` (warning) | **none / no_fault** | gateway > 35 rps **and** gateway 5xx ratio < 0.02 | ✅ |

**Scoring** (SPEC §8): a run is correct iff all three hold:
- `root_cause_service` ∈ {expected} ∪ `accepted_services`;
- `fault_type` matches;
- no `must_not_blame` service is named as the root cause.

`suspect_commit: from_deploy` means: the commit created by the scenario's deploy step. It is resolved from `data/deploy_events.jsonl` and reported separately as commit accuracy.

s11 and s12 are the most important for the defense. They show the system does not simply blame the loudest red graph or the latest deploy.

## How one scenario run works

`make scenario S=sNN` (= `python -m chaos scenario sNN`):

1. **Reset** to the healthy baseline:
   - scenario toxics removed (the baseline DB latency stays);
   - stopped containers started, in-process faults off;
   - `target-repo` back at the `baseline` tag, with changed services rebuilt;
   - deploy log cleared.
2. **Two noise deploys:** harmless real releases (a reworded notification text, a refactor in payment, a cache TTL change in inventory). They are always in services the scenario's ground truth does not involve, so "blame the latest deploy" is a losing strategy. The Change Investigator sees them exactly like real deploys.
3. **Scenario load:** if `load.rps` is above the always-on 10 rps, an extra load generator adds the difference.
4. **Warm-up** (`warmup_s`, 120 s): healthy traffic, so the agents have a baseline to compare against.
5. **Inject** the fault (setup steps).
6. **Observe** (`observe_s`, 90 s): the symptom develops.
7. **Symptom check:** each PromQL condition is evaluated and printed ✓/✗. A ✗ means the scenario itself failed, not the agents.
8. **Alert:** `alerts/sNN.json`, with `started_at` = injection time + 30 s, as a real alert rule with a `for:` clause would fire. It contains only the symptom.

Then `make investigate ALERT=alerts/sNN.json` and, afterwards, `make reset`.

## Setup step types

| Step | Example | Mechanism |
|---|---|---|
| `toxic` | `{proxy: gateway_payment, type: latency, latency_ms: 2000}` | Toxiproxy API (proxies in `infra/toxiproxy/toxiproxy.json`) |
| `docker` | `{stop: postgres}` | `docker compose stop` |
| `chaos` | `{service: notification, leak_memory: true}` | `POST /admin/chaos` on the service's internal port 9000, via `docker compose exec` |
| `deploy` | `{service: order, patch: s04_order_promotions.patch}` | deploy simulator: `git apply` → VERSION bump → commit (realistic author) → rebuild → deploy event |
| `load_spike` | `{extra_rps: 40, duration_s: 300}` | an extra load generator starting at injection time |

## Validation (Phase 2)

All of s01–s08 were run on 12 Oct 2026 and s09–s12 on 17 Oct 2026; every symptom check passed. s05 and s06 passed after two harness fixes, described in [results.md](results.md#phase-2-scenario-validation), which also has the measured values. `make reset` returned the system to healthy after each run.
