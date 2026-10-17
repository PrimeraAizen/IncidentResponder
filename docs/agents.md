# Agents

Seven agents, each with **one** role. Every LLM agent is the same bounded loop (`agents/responder/runtime.py`) with its own prompt (`agents/responder/agents/prompts/<agent>.md`), its own toolset (at most 5 tools) and a typed output (`agents/responder/schemas.py`).

## Agent table

| # | Agent | Role (one sentence) | Input | Output | Tools (≤5) | Completion criterion | Status |
|---|---|---|---|---|---|---|---|
| 1 | **Orchestrator** | Turns the alert into an investigation plan and assigns tasks; does no analysis. | `Alert` | `InvestigationPlan` (time window, focus services, one `Task` per specialist) | none | Valid plan produced (1 LLM call); on failure the deterministic planner's plan is used | ✅ |
| 2 | **Log Analyst** | Finds error patterns, their onset time and affected services in logs. | `Task` | `EvidenceBundle` (source `logs`) | `loki_query`, `loki_label_values`, `error_pattern_summary` | ≥1 evidence item or explicit "no anomalies", within budget | ✅ |
| 3 | **Metrics Analyst** | Finds anomalies in latency, error rate, resources, pool/cache/event metrics. | `Task` | `EvidenceBundle` (source `metrics`) | `prom_query`, `prom_query_range`, `zscore_anomaly`, `targets_health` | ≥1 evidence item or explicit "no anomalies", within budget | ✅ |
| 4 | **Change Investigator** | Finds deploys, commits, config changes and container restarts near the incident window. | `Task` | `EvidenceBundle` (source `changes`; a deploy's commit sha in `raw_ref`) | `deploy_events`, `git_log`, `git_diff`, `docker_inspect` | ≥1 evidence item or explicit "no changes", within budget | ✅ |
| 5 | **Diagnostician** | Combines all evidence into ranked root-cause hypotheses. | all `EvidenceBundle`s (+ previous `Verdict` on retry) | `HypothesisSet` (ranked, ≤3) | `dependency_graph` | ≥1 hypothesis citing existing evidence ids | ✅ |
| 6 | **Verifier** | Independently tests the top hypothesis against fresh queries; accepts or rejects with reasons. | top `Hypothesis` + `InvestigationPlan` | `Verdict` (confirmed / rejected / inconclusive) | `prom_query`, `loki_query`, `health_check` | Verdict with ≥1 check performed | ✅ |
| 7 | **Reporter** | Writes the final incident report and recommended actions. | plan, evidence, hypotheses, verdict | `IncidentReport` (+ Markdown file) | `write_report` (run by the Reporter's code once the report validates) | Report saved to `reports/` | ✅ |

## Why the roles do not overlap

| Agent | Reads | Does not |
|---|---|---|
| Orchestrator | only the alert | look at any data; it decides *what* to look at, not *what it means* |
| Log Analyst | only Loki | read metrics or changes; does not name a root cause |
| Metrics Analyst | only Prometheus | read logs or changes; does not name a root cause |
| Change Investigator | only git, the deploy log, Docker state | read logs or metrics; does not name a root cause |
| Diagnostician | only the specialists' evidence (+ static topology) | query live data; it *generates* hypotheses |
| Verifier | fresh Prometheus/Loki/health data for **one** hypothesis | generate hypotheses; it only *tests* them (no self-confirmation) |
| Reporter | the final state | analyze or query anything; it *writes* |

Specialists report **observations** (with `service` = the component that emitted or exported the signal). Only the Diagnostician turns observations into causes. Only the Verifier judges a cause.

## Call budget per typical run (target: every agent ≤ 40% of all calls)

| Agent | LLM calls | Tool calls | Hard caps (env) |
|---|---|---|---|
| Orchestrator | 1 | 0 | — |
| Log / Metrics / Change (each) | 1–3 | 2–4 | `MAX_LLM_CALLS_PER_AGENT=3` (+1 repair), `MAX_TOOL_CALLS_PER_AGENT=4` |
| Diagnostician | 1–2 per loop | 1 | same |
| Verifier | 1–2 per loop | 2–3 | same, `MAX_VERIFY_LOOPS=2` |
| Reporter | 1 | 1 | same |

Expected total ≈ 25–35 calls; the largest agent ≈ 15–25%. The CLI prints the per-agent shares after each run. `eval/load_report.py` (Phase 4) computes them from the JSONL call logs.

Measured on a full 7-agent run (s01, `gemini-3.1-flash-lite`, 17 Oct 2026; `make load-report`), 27 calls in total:

| Agent | LLM calls | Tool calls | Share |
|---|---|---|---|
| metrics_analyst | 3 | 4 | 26% |
| change_investigator | 3 | 3 | 22% |
| verifier | 3 | 2 | 19% |
| log_analyst | 2 | 1 | 11% |
| diagnostician | 2 | 1 | 11% |
| reporter | 1 | 1 | 7% |
| orchestrator | 1 | 0 | 4% |

The largest share is 26%, well under the 40% limit. The evaluation's typical-run table is in [results.md](results.md).

## Tools

| Tool | Used by | Data source | What it returns |
|---|---|---|---|
| `loki_query` | Log Analyst, Verifier | Loki `/query_range` | newest matching lines, compacted to key fields (≤40) |
| `loki_label_values` | Log Analyst | Loki `/label/<l>/values` | e.g. which services have logs |
| `error_pattern_summary` | Log Analyst | Loki | warning/error lines grouped into normalized patterns: count, first/last seen, example (top 10) |
| `prom_query` | Metrics Analyst, Verifier | Prometheus `/query` | value per series at one instant |
| `prom_query_range` | Metrics Analyst | Prometheus `/query_range` | per-series min/max/mean/last + biggest level shift (when, before/after) |
| `zscore_anomaly` | Metrics Analyst | Prometheus `/query_range` + Python | per-series baseline vs incident mean, ratio, z-score (most anomalous first) |
| `targets_health` | Metrics Analyst | Prometheus `/targets` | up/down per service |
| `deploy_events` | Change Investigator | `data/deploy_events.jsonl` | deploys in the window, with `vs_alert_s` |
| `git_log`, `git_diff` | Change Investigator | `target-repo` git (read-only: `log`, `show`) | commits with changed files; one commit's diff (truncated) |
| `docker_inspect` | Change Investigator | Docker API (read-only; system components only) | state, health, restarts, OOM-killed, start/stop time |
| `dependency_graph` | Diagnostician | `agents/responder/topology.yaml` | components and who calls whom |
| `health_check` | Verifier | service `/healthz` | status code, latency, dependency checks; for postgres/redis/nats the checks of their dependents |
| `write_report` | Reporter | `reports/` | paths of the saved Markdown + JSON |

All tools: typed Pydantic inputs, compact JSON outputs, read-only except `write_report`, retried on transient errors, every call logged. Every timestamp in a tool result comes with `vs_alert_s` (seconds relative to the alert, negative = before), so the LLM never does date arithmetic. Registry: `agents/responder/tools/__init__.py`. Allow-list test: `agents/tests/test_tools_registry.py`.
