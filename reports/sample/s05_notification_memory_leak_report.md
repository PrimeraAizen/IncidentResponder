# Incident inc-20261027-220814-7498: notification / memory_leak

**Status:** complete · **Confidence:** 1.00

## Summary

Users experienced service instability in the notification service due to a rapid, sustained increase in heap memory usage. This memory leak triggered the HighMemoryUsage alert and caused multiple container restarts. The team has high confidence in this diagnosis as the memory growth was observed directly in metrics.

## Timeline

- 22:06:44 - Heap memory usage increased from 2.57MB to 53.29MB
- 22:07:14 - HighMemoryUsage alert triggered
- 22:07:44 - Notification container restarted
- 22:08:44 - Verification confirmed 36x memory increase and multiple process restarts

## Recommended actions

1. Perform a heap dump analysis to identify the specific objects causing the memory leak.
2. Implement memory usage limits and proactive alerts for heap growth thresholds.
3. Add automated integration tests to monitor memory consumption during service operation.
4. Review recent dependency updates for potential memory management regressions.

## Verification

Verifier verdict: **confirmed**. The notification service shows a massive, rapid increase in heap memory usage and multiple recent process restarts, confirming the memory leak hypothesis. No error logs were found, consistent with an internal memory management issue rather than external failures.

- heap memory usage increased by ~36x compared to 4 minutes ago
- 2 process restarts detected in the last 10 minutes

## Evidence

| id | source | service | signal |
|---|---|---|---|
| changes-1 | changes | notification | notification container restarted 1x, 29 s after the alert (started_at 2026-10-27T22:07:44Z) |
| metrics-1 | metrics | notification | heap memory usage increased from 2.57MB baseline to 53.29MB during incident (z=100) |
| metrics-2 | metrics | notification | notification service experienced container restarts (restarts_5m metric) |
| verify-1 | verification | notification | heap memory usage increased by ~36x compared to 4 minutes ago |
| verify-2 | verification | notification | 2 process restarts detected in the last 10 minutes |
