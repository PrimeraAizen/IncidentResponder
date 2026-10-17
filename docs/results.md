# Results

This file collects measured results:
- the evaluation of the agents (`make eval`, written by `eval/run_all.py`);
- the per-agent load distribution of a typical run (`eval/load_report.py`);
- the harness's own validation of every scenario.

## Evaluation: accuracy per scenario

A run is correct when the reported root-cause service and fault type match the scenario's ground truth and no `must_not_blame` service is named (SPEC §8). A scenario counts as solved when the majority of its repeats are correct. "commit" = suspect-commit accuracy for scenarios caused by a deploy.

<!-- eval:start -->
Run on 2026-10-17 with `gemini-3.1-flash-lite`, 1 repeat(s) per scenario.

**Scenarios correct (majority of repeats): 9/9** · runs correct: 9/9 (100%) · mean investigation time: 38 s · mean tokens per run: 40,597

| scenario | expected | reported (per run) | correct | majority | commit | time s | LLM | tools | tokens |
|---|---|---|---|---|---|---|---|---|---|
| s01_payment_latency | payment / latency | payment/latency | 1/1 | ✅ | - | 12 | 15.0 | 13.0 | 40,027 |
| s02_postgres_down | postgres / dependency_down | postgres/dependency_down | 1/1 | ✅ | - | 49 | 13.0 | 13.0 | 39,715 |
| s03_redis_down | redis / dependency_down | redis/dependency_down | 1/1 | ✅ | - | 134 | 19.0 | 17.0 | 50,003 |
| s04_order_bad_deploy | order / bad_deploy | order/bad_deploy | 1/1 | ✅ | 1/1 | 12 | 13.0 | 13.0 | 37,910 |
| s05_notification_memory_leak | notification / memory_leak | notification/memory_leak | 1/1 | ✅ | - | 47 | 14.0 | 14.0 | 32,075 |
| s06_order_pool_exhaustion | order / resource_exhaustion | order/resource_exhaustion | 1/1 | ✅ | 1/1 | 12 | 14.0 | 14.0 | 39,464 |
| s07_nats_down | nats / dependency_down | nats/dependency_down | 1/1 | ✅ | - | 24 | 15.0 | 15.0 | 42,176 |
| s08_gateway_misconfig | gateway / misconfiguration | gateway/misconfiguration | 1/1 | ✅ | 1/1 | 22 | 15.0 | 13.0 | 43,077 |
| s09_order_slow_query | postgres / slow_query | postgres/slow_query | 1/1 | ✅ | 1/1 | 31 | 15.0 | 13.0 | 40,929 |
<!-- eval:end -->

## Load distribution of a typical run

No agent may perform more than 40% of all LLM + tool calls (SPEC 2.4). Retried attempts are counted once.

<!-- load-report:start -->
Call log: `logs/sample/typical_run.jsonl (s06_order_pool_exhaustion, run 1)` (28 calls). Limit: no agent above 40%.

| agent | LLM calls | tool calls | total | share | ≤ 40% |
|---|---|---|---|---|---|
| verifier | 3 | 4 | 7 | 25% | ✅ |
| change_investigator | 3 | 3 | 6 | 21% | ✅ |
| metrics_analyst | 2 | 4 | 6 | 21% | ✅ |
| log_analyst | 2 | 1 | 3 | 11% | ✅ |
| diagnostician | 2 | 1 | 3 | 11% | ✅ |
| reporter | 1 | 1 | 2 | 7% | ✅ |
| orchestrator | 1 | 0 | 1 | 4% | ✅ |
<!-- load-report:end -->

## Tuning history

### Pass 1 (17 Oct 2026, `gemini-3.1-flash-lite`, 1 run per scenario): 9/12 correct

| scenario | result | what happened |
|---|---|---|
| s01–s08, s11 | ✅ | correct. Deploy commits found in s04, s06, s08. In s07 the verify loop worked: first hypothesis rejected, second confirmed. |
| s09 slow query | ❌ nats / latency | The Diagnostician was **right at first** (postgres / slow_query, 0.9). The Verifier measured the query p95 at 94 ms and called it "normal": it had no baseline (before the incident the value was ~2 ms). Its rejection pointed to NATS errors that were left over from the previous scenario (s07). |
| s10 goroutine leak | ❌ redis / latency | The Metrics Analyst never looked at goroutines: its last call went to DB metrics, where it found only a side effect. |
| s12 no fault | ❌ (failed report) | The previous scenario's (s11) payment timeouts were inside the 10-minute window, in logs and metrics. The Diagnostician blamed payment, the Verifier correctly rejected that, then the Diagnostician hit the 180 s time limit. |

Fixes (all general, none mentions a scenario):
1. **Harness:** a 150 s cooldown between evaluation runs, and a 5-minute default look-back. Back-to-back scenarios had put the previous fault inside the next window, which is an artifact of compressed test time.
2. **Agents tell earlier, resolved events from the current one:**
   - the Log Analyst does not report patterns that stopped > 60 s before the alert;
   - the Metrics Analyst and Diagnostician reason only about changes that start near the alert.
3. **The Verifier always compares with before the incident:** code gives it a safe PromQL `offset`, e.g. `X / (X offset 6m)`. "Never call a value normal without a comparison."
4. **The Metrics Analyst runs four combined queries in its first turn,** covering latency, dependencies (calls + DB queries + pool wait), errors, and resources/traffic. Every fault class is covered in one round.
5. **Final decision:** when the Verifier rejects every hypothesis it tested, the report uses the Diagnostician's most confident one (marked partial), not simply the last. A Diagnostician failure inside the loop keeps the earlier hypotheses.
6. **`RUN_TIMEOUT_S=300`** for the free tier (single LLM calls up to 49 s); tool arguments are now in the call log for audits.

<!-- pass2:start -->
### Pass 2 (targeted: the three failures + two regression checks): 3/5 correct

| scenario | result | what happened |
|---|---|---|
| s10 goroutine leak | ✅ fixed | Query D showed inventory's goroutines in the thousands; diagnosed and confirmed. |
| s12 no fault | ✅ fixed | No contamination any more: "only traffic rose ×5, no errors" → none / no_fault, confirmed. |
| s01 | ✅ | still correct |
| s09 slow query | ❌ order / bad_deploy | Right component (order is accepted) and right commit, wrong fault type. The Diagnostician's deploy rule mapped every code change to bad_deploy; schema/index migrations were not covered. |
| s11 retry storm | ❌ payment / goroutine_leak | Right component, wrong fault type: payment's goroutines rose 11 → 39 while requests waited, and that was read as a leak (s10's real leak went 30 → 18,000). The Verifier confirmed it. |

Fixes (general taxonomy, both for the Diagnostician and the Verifier):
- A migration or schema/index change that slows database queries is **slow_query at postgres**, even when a service's deploy shipped it.
- A **goroutine leak** means 10× or more and still growing. A few times more goroutines while requests wait is a symptom of latency.
- The Verifier rejects a hypothesis whose component is right but whose fault type the data contradicts.
<!-- pass2:end -->

### Pass 3 (targeted: s09, s11 + leak scenarios s10, s05): 3/4, then 4/4

| scenario | result | what happened |
|---|---|---|
| s09 slow query | ✅ fixed | postgres / slow_query with the migration's commit, confirmed |
| s11 retry storm | ✅ fixed | payment / latency, confirmed: the modest goroutine rise was not called a leak |
| s10 goroutine leak | ✅ | still correct (the leak rule kept the real leak) |
| s05 memory leak | ❌ → ✅ | The Verifier compared heap *now* with before. After the OOM kill the new process had little memory, so it saw "no leak"; the Diagnostician then called it a transient spike (no_fault). **A restart erases its own evidence.** The Verifier now compares the *peak* (`max_over_time`) of the incident with before and counts restarts, and "a restart or OOM kill is never no fault". Re-run: notification / memory_leak ✅. |

## Scenario validation (harness)

Each scenario was run with `make scenario S=sNN` (s01–s08 on 12 Oct, s09–s12 on 17 Oct 2026) (Apple Silicon, Docker Desktop with 8 GB). "Measured" is the value of the scenario's symptom check at the end of `observe_s` (90 s after injection). Every run started from `make reset` and two noise deploys. The next run's reset returned the system to healthy (gateway `/healthz` 200, all containers running, `target-repo` at `baseline`).

| # | Symptom check | Threshold | Measured | ✓ |
|---|---|---|---|---|
| s01 | gateway `POST /api/orders` p95 latency | > 1 s | 2.95 s | ✅ |
| s02 | gateway 5xx ratio | > 0.2 | 1.00 | ✅ |
| s03 | inventory `GET /products` p95 latency | > 0.05 s | 0.97 s | ✅ |
|     | inventory cache errors | > 0.5/s | 5.9/s | ✅ |
| s04 | gateway 5xx ratio | > 0.1 | 0.22 | ✅ |
| s05 | notification peak heap (3 min) | > 50 MB | 117 MB | ✅ |
|     | notification restarts (3 min) — OOM kill | > 0 | 1 | ✅ |
| s06 | gateway `POST /api/orders` p95 latency | > 1 s | 2.95 s | ✅ |
|     | order DB pool wait p95 | > 0.2 s | 2.88 s | ✅ |
| s07 | failed `orders.created` publishes | > 0.5/s | 2.98/s | ✅ |
|     | gateway 5xx ratio (orders must still succeed) | < 0.05 | 0.00 | ✅ |
| s08 | gateway 5xx ratio | > 0.1 | 0.29 | ✅ |
| s09 | order `recent_orders` query p95 | > 0.03 s | 0.098 s | ✅ |
|     | order `POST /orders` p95 latency | > 0.2 s | 0.24 s | ✅ |
| s10 | inventory goroutines | > 5000 | 18,060 | ✅ |
|     | gateway `GET /api/products` p95 latency | > 0.05 s | 0.23 s | ✅ |
| s11 | gateway 5xx ratio | > 0.1 | 0.30 | ✅ |
|     | calls to payment per order (retry storm) | > 2 | 4.07 | ✅ |
| s12 | gateway requests per second | > 35 | 50.0 | ✅ |
|     | gateway 5xx ratio (nothing is broken) | < 0.02 | 0.00 | ✅ |

What the first attempt revealed, and was fixed:
- **s05:** the heap reached 190 MB, but the 128 MiB container was not killed. Docker allows as much swap as memory by default, so the container swapped. Fixed with `memswap_limit: 128m`.
- **s06:** with the normal request mix (30% orders) a 2-connection pool was only ~40% busy at 30 rps. The scenario now models a lunch-time order rush: 40 rps with 70% orders, which needs ~2.4 connections on average. The default pool of 10 still handles it at ~24% utilization.
- **s06, second attempt:** the fault worked so well that the new order container never became *healthy*: `/healthz` pings Postgres through the exhausted pool. The deploy step waited for health and aborted. Deploys now wait only until the container runs, as a real pipeline without health gating would. The reset still waits for health.

s05 and s06 were re-run after these fixes. The other six passed on the first attempt.
