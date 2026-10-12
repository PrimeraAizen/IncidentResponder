# Results

This file collects measured results. Phase 4 adds the evaluation table (accuracy per scenario over 3 repeats, latency, tokens) and the per-agent load distribution from `eval/load_report.py`.

## Phase 2: scenario validation

Each scenario was run with `make scenario S=sNN` on 12 Oct 2026 (Apple Silicon, Docker Desktop with 8 GB). "Measured" is the value of the scenario's symptom check at the end of `observe_s` (90 s after injection). Every run started from `make reset` and two noise deploys. The next run's reset returned the system to healthy (gateway `/healthz` 200, all containers running, `target-repo` at `baseline`).

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

What the first attempt revealed, and was fixed:
- **s05:** the heap reached 190 MB, but the 128 MiB container was not killed. Docker allows as much swap as memory by default, so the container swapped. Fixed with `memswap_limit: 128m`.
- **s06:** with the normal request mix (30% orders) a 2-connection pool was only ~40% busy at 30 rps. The scenario now models a lunch-time order rush: 40 rps with 70% orders, which needs ~2.4 connections on average. The default pool of 10 still handles it at ~24% utilization.
- **s06, second attempt:** the fault worked so well that the new order container never became *healthy*: `/healthz` pings Postgres through the exhausted pool. The deploy step waited for health and aborted. Deploys now wait only until the container runs, as a real pipeline without health gating would. The reset still waits for health.

s05 and s06 were re-run after these fixes. The other six passed on the first attempt.
