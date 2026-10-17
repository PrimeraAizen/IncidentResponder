# Incident inc-20261017-141826-f758: order / resource_exhaustion

**Status:** complete · **Confidence:** 1.00 · **Suspect commit:** `8db76fe`

## Summary

Users experienced high latency and timeouts when accessing the /api/orders endpoint due to a critical resource exhaustion issue. The incident was caused by a deployment that incorrectly reduced the database connection pool size in the order service. The team has 100% confidence in this diagnosis based on configuration diffs and performance metrics.

## Timeline

- 14:16:56 - order v1.4.3 deployed with reduced DB pool size
- 14:17:26 - HighLatency alert triggered for gateway service
- 14:17:54 - Order service logs report database connection timeouts
- 14:18:33 - Verification confirms 161x increase in connection acquisition latency

## Recommended actions

1. Roll back order service to the previous stable version (v1.4.2) to restore the original database connection pool size.
2. Implement automated load testing in the CI/CD pipeline to detect performance regressions caused by configuration changes.
3. Add alerts for database connection pool saturation and acquisition latency to detect resource exhaustion before it impacts end users.

## Verification

Verifier verdict: **confirmed**. The database connection pool size was confirmed to have been reduced from 10 to 2 following the deployment. This reduction caused a massive spike in connection acquisition latency (161x), confirming the resource exhaustion hypothesis.

- db_pool_max_conns reduced from 10 to 2
- db_pool_acquire_duration p95 increased by factor of 161.9

## Evidence

| id | source | service | signal |
|---|---|---|---|
| changes-1 | changes | order | order v1.4.3 deployed 30 s before the alert (commit 8db76fe by D. Bekov, 'order: lower DB pool size...'); diff reduces DB max_conns from 10 to 2 |
| logs-1 | logs | order | 724 ERROR 'database query failed' lines from order service with upstream=postgres, error='timeout: context deadline exceeded' or 'acquire connection: context deadline exceeded', first at 14:17:54Z. |
| logs-2 | logs | gateway | 725 ERROR 'http request' lines for /api/orders with status=502 and latency_ms ≈ 2000, first at 14:17:54Z. |
| metrics-1 | metrics | order | db-pool-wait latency increased from 0.009s to 2.63s (z=100) |
| metrics-3 | metrics | gateway | p95 latency for /api/orders increased from 0.24s to 2.76s (z=100) |
| verify-1 | verification | order | db_pool_max_conns reduced from 10 to 2 |
| verify-2 | verification | order | db_pool_acquire_duration p95 increased by factor of 161.9 |
