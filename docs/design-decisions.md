# Design decisions

## 1. Why a multi-agent system (and not one agent)

**Three independent data sources call for three narrow specialists.**
- Logs (Loki), metrics (Prometheus) and changes (git, deploy log, Docker) need different tools, query languages and reasoning.
- One agent would need ~14 tools. The assignment allows at most 5, and LLMs choose tools worse as the toolset grows.
- With one specialist per source, each agent has ≤4 tools and a short, focused prompt. The three specialists are independent, so they **run in parallel**, which cuts wall-clock time.

**Generating a hypothesis is separated from testing it.**
- The Diagnostician proposes causes. The Verifier tests the top one with *fresh* queries it chooses itself.
- An agent that both proposes and checks its own idea tends to confirm it (self-confirmation bias). Two roles with different prompts and inputs reduce that.
- A rejected hypothesis goes back to the Diagnostician (at most 2 loops).

**Small contexts mean fewer hallucinations and easier testing.**
- Each specialist sees only its own task and tool results, never the whole incident.
- Each agent can be tested in isolation with the fake LLM (`agents/tests/test_graph_fake.py`).

**Load is distributed by construction.**
- The Orchestrator makes 1 LLM call; routing between agents is code.
- No agent can dominate the call count, which keeps the "≤40% of calls per agent" requirement easy to meet and to prove from the logs.

**Trade-offs we accept:**
- More LLM calls in total than a single agent: roughly 25–35 per run.
- Structured hand-offs lose some nuance compared with one long chain of thought.

The single-agent baseline (stretch goal, `eval/baseline_single_agent.py`) will measure this trade-off empirically.

## 2. Why these roles

| Role | Why it exists |
|---|---|
| Orchestrator | Converts a vague alert into concrete, bounded tasks (window, focus services). Kept tiny: 1 call, no tools, no analysis. |
| Log Analyst / Metrics Analyst / Change Investigator | One per data source (see above). They report **observations**, never causes. |
| Diagnostician | The only agent that reasons about causality: combines evidence with the dependency graph. |
| Verifier | Independent check, needed for the top grading band and against confirmation bias. |
| Reporter | Separates writing from analysis, so the report never invents new findings. |

## 3. Architectural decisions

| Decision | Reason |
|---|---|
| **Routing in code** (LangGraph edges), not by an LLM | Deterministic, testable control flow; the Orchestrator stays at 1 call. |
| **Pydantic messages**, validated at every hand-off | A malformed answer never propagates. It gets one repair call, then a typed `AgentFailure`. |
| **Code owns ids and bookkeeping fields** (`evidence_id`, `agent`, `task_id`, `hypothesis_id`) | LLMs collide or invent ids. The Diagnostician's references to unknown ids are dropped, and it must cite at least one real one. |
| **Tools return summaries**, not raw data (pattern counts, z-scores, level shifts) | Keeps every tool result under ~1.5k tokens. The math (z-score, change point, log normalization) is plain Python that can be unit-tested and explained. |
| **Time arguments of tools are optional** and default to the task window | Shorter tool calls and no date-arithmetic mistakes by the LLM. |
| **Specialists set `service` = the component that emitted/exported the signal** | Observations stay objective. Attributing the cause is the Diagnostician's job. |
| **Last LLM call of an agent runs with tools disabled** | The agent always ends with an answer within `MAX_LLM_CALLS_PER_AGENT`. A repair call (if needed) is the only call allowed beyond the cap. |
| **One retry wrapper** (`CallLogger.call`, tenacity, 3 attempts, exponential backoff) for both LLM and tool calls | "Every call is retried and logged" holds by construction. The OpenAI SDK's own retries are disabled so that no retry goes unlogged. |
| **Errors the LLM can fix are not retried** (bad PromQL, 4xx); they go back to the LLM as `{"error": …}` | The agent can correct its query within its budget. |
| **A failing agent degrades the run instead of crashing it** | Its bundle is marked `failed`. The Diagnostician is told which source is missing and lowers its confidence. |

## 4. Anti-leakage decisions

The agents must find the root cause only from what an on-call engineer has.

| Decision | Reason |
|---|---|
| Tool registry allow-list + test (`test_tools_registry.py`) | No tool can read `scenarios/`, `chaos/`, `/admin/chaos`, the Toxiproxy API or harness state. |
| **Alloy ships logs only from system components** (gateway, order, payment, inventory, notification, postgres, redis, nats) | Toxiproxy's own logs say "added toxic latency to gateway_payment". Shipping them would hand the answer to the Log Analyst. Loadgen logs are client-side noise. |
| The alert file contains only the symptom (no scenario id, no labels from the YAML `expected` block) | Ground truth is used only for scoring. |
| Agents never get a tool that writes anything except the report | Non-goal: no autonomous remediation. |

## 5. Choices made where the spec was silent (Phase 1)

Following the rule "choose the simplest reasonable option and note it":

1. **Repository root** = the directory containing `SPEC.md` (no extra `incident-responder/` sub-folder). The GitHub repository can be named `incident-responder`.
2. **One Go module** (`module shop`) for all services and `pkg/obs`, with **one Dockerfile** taking a `SERVICE` build arg.
   - The build context is the target-repo root, so every service can import `pkg/obs`.
   - `VERSION` comes from `<service>/VERSION` (deploy patches bump it like a real release). `COMMIT` is the target-repo HEAD, passed as a build arg.
   - Both end up in the `build_info{service,version,commit}` metric.
3. **Small libraries beyond the stack table:**
   - `gopkg.in/yaml.v3`: the services' `config.yaml`;
   - `PyYAML`: scenarios, topology;
   - `python-dotenv`: reads `.env`.

   They are libraries, not frameworks.
4. **The load generator was pulled forward from Phase 2.** s01 needs traffic to have a baseline and a symptom.
   - It runs as an always-on compose service (`LOADGEN_RPS`, default 10 rps), which mimics production traffic.
   - Its Phase 1 mix is `create=75,get=25`, because the product listing needs inventory (Phase 2).
5. **Optional dependencies in Phase 1:**
   - The gateway skips the inventory step when `inventory_url` is empty (default price 9.99).
   - Order has no NATS publishing yet.

   Phase 2 fills these in through config.
6. **Deterministic planner** (`agents/planner.py`) instead of the Orchestrator in Phase 1.
   - It builds the same `InvestigationPlan`: window = alert − 15 min … now, focus = alerting service + its transitive dependencies.
   - From Phase 3 it remains the fallback when the Orchestrator's LLM call fails.
7. **Slow upstream calls (> 1 s) are logged as WARN** by `pkg/obs`, in addition to failures. This is a common production practice, and it gives the Log Analyst a signal for pure-latency faults, which produce no errors.
8. **`/healthz` includes dependency checks** (order pings Postgres). A service whose database is down reports `degraded` (503), as a real readiness probe would.
9. **`zscore_anomaly` details:**
   - Default windows: baseline = the 5 min ending 60 s before the alert; incident = from 30 s before the alert to now.
   - The standard deviation is floored at 10% of the baseline mean, so perfectly stable baselines do not produce absurd z-scores.
   - |z| is capped at 100.
   - A series absent from the baseline (e.g. a new 5xx status) is reported as "new".
10. **s01 `warmup_s` raised from 60 to 120 s.** The z-score baseline needs a few minutes of healthy data before the fault. With 60 s and a fresh stack, the baseline was empty.
11. **Load accounting counts logical calls.** Failed attempts that were retried appear in the log (`status: "retry"`) but are not counted twice in the per-agent shares.
12. **Lenient validators:**
    - `strength` and `confidence` are clamped to 0..1;
    - naive datetimes are treated as UTC;
    - more than 3 hypotheses are truncated.

    Small formatting slips should not cost a whole run.
13. **Host ports are bound to 127.0.0.1.**
    - Postgres is not published at all: port 5432 is often taken on developer machines.
    - Only Toxiproxy's control port is published, for the chaos CLI.
14. **Python 3.12** is pinned in `agents/.python-version`. `uv` downloads it, so the defense machine runs exactly what was tested.
15. **Fake provider outputs are marked `[fake]`,** so a canned answer is never mistaken for real reasoning (e.g. in screenshots).
16. **The provider's assistant message is sent back unchanged** (`LLMResponse.raw_message`), instead of being rebuilt from our normalized fields. Gemini 3 attaches a `thought_signature` to each tool call and rejects the next request with 400 if it is missing. We found this on the first real run.
17. **Client-side rate limit `LLM_MAX_RPM`** (sliding 60 s window, shared by the parallel agents). Free tiers allow very few requests per minute (Gemini: 5). Pacing the calls is better than collecting 429s. The time spent waiting is excluded from the logged LLM latency.
18. **LLM retries honour the provider's hint.** A 429 that says "retry in 30.9s" (or sends a `Retry-After` header) waits that long, capped at `LLM_MAX_RETRY_WAIT_S`. Otherwise the waits are 2 s, then 4 s. `AgentFailure.attempts` reports the real number of attempts: a non-retryable 400 counts as 1, not 3.

## 6. Choices made in Phase 2 (full backend & harness)

19. **A baseline network latency to Postgres (20 ± 5 ms per round trip) is part of the normal environment.** It is a Toxiproxy toxic named `baseline_latency`, applied by `make up` and restored by every reset; see `infra/toxiproxy/baseline/README.md`.
    - **Why:** locally a round trip takes ~0.3 ms, so even a 2-connection pool never runs out and s06 (pool exhaustion) could not happen.
    - **Effect:** with 20 ms per round trip an order holds a connection for ~80 ms (BEGIN, fraud check, INSERT, COMMIT). The default pool of 10 copes; a pool of 2 at 30 rps does not. This is how pool exhaustion happens in production too.
20. **Orders are created in one transaction** (fraud check + insert). This is correct for consistency, and it is what makes connection hold time realistic.
21. **Noise deploys happen at the start of `make scenario`, not in `make reset`.** The spec asks for 1–2 harmless deploys "a few minutes before each scenario", in an *unrelated* service, and only the scenario knows which services are related.
    - The default is two noise deploys from `notification → payment → inventory`, skipping the ground-truth services.
    - A scenario can override the choice (s08 does: a payment restart would produce the same `upstream=payment` errors as its fault).
    - `make reset` returns to a clean baseline with an empty deploy log.
22. **`/admin/chaos` is served on a separate internal port (9000)** that is not published to the host. The harness reaches it with `docker compose exec`. Toggling a fault is deliberately **not logged**: the agents must find a leak from its effects (heap, goroutines, restarts), not from a log line saying "leak enabled".
23. **Faults are realistic, not scripted:**
    - **leak_memory:** keeps 2 MiB/s reachable. Notification has a 128 MiB memory limit, so the container is really OOM-killed and restarted by Docker.
    - **leak_goroutines:** starts 200 goroutines/s that never exit and periodically do a little work, so CPU use and latency grow with their number.
    - **cpu_burn:** keeps every core busy.
24. **Panics are recovered and logged as JSON** (`msg="panic recovered"`, `error`, `location`) with a 500 response, like most web frameworks do. Without this, s04's nil-pointer bug would only appear as dropped connections with no log line.
25. **Inventory's cache fallback is genuinely slower.** The Postgres path also counts each product's orders of the last 7 days (an aggregate over the orders table), and go-redis keeps its default retries with backoff before giving up. Losing Redis (s03) therefore shows up as latency, as it would in production.
26. **NATS publishes fail fast while disconnected** (`ReconnectBufSize(-1)`), instead of silently buffering in memory. s07 then produces `event publish failed upstream=nats` errors and `events_published_total{status="error"}`, while orders still succeed.
27. **Alloy refreshes container discovery every 5 s** (default 60 s). A deploy recreates the container with a new id, and the default would lose the first minute of the new version's logs, which is exactly the evidence of a bad deploy.
28. **`zscore_anomaly`'s default baseline is the 2 minutes ending 60 s before the alert** (Phase 1 used 5 min). When scenarios run back-to-back, a longer baseline would include the previous scenario's fault.
29. **Every scenario has a harness-only `symptom` check** (PromQL + threshold). It proves the injected fault produced the intended symptom, so a failed evaluation run can be told apart from a broken scenario.
30. **`load.rps` is the total request rate during a scenario.** The always-on load generator provides `LOADGEN_RPS` (10). The harness starts a second one for the difference, from the warm-up until `duration_s` after injection.
31. **Expected deploy commits are resolved at run time** (`suspect_commit: from_deploy`): the sha is created by the deploy step, so it cannot be written in the YAML.
32. **s08 adds `must_not_blame: [payment]`.** The fault produces "payment unreachable" errors at the gateway, but payment itself is healthy. Blaming it is precisely the mistake this scenario tests.
33. **Deploys do not wait for the new version to become healthy.** A broken release may never become healthy (s06: `/healthz` pings Postgres through the exhausted pool), and a pipeline without health gating would not wait either. Resets do wait for health.
34. **s06 runs under a lunch-time order rush** (`load: {rps: 40, mix: "create=70,get=30"}`). With the normal mix (30% orders), even 2 connections are only ~40% busy. Pool exhaustion needs demand above capacity, which is also what happens in production.

## 7. Choices made in Phase 3 (all agents end-to-end)

35. **The Orchestrator decides only planning choices:** how far back to look (`lookback_minutes`, clamped to 5–30), which services to focus on, and a tailored objective per specialist.
    - Code fills in the incident id, exact timestamps, task ids and the fixed list of three specialists.
    - LLMs are bad at date arithmetic, and a plan must never omit a specialist.
    - If its call fails, the deterministic planner's plan is used, so a run never stops at step 1.
36. **Routing rules are code** (`after_diagnostician`, `after_verifier` in `graph.py`):
    - A rejected hypothesis goes back to the Diagnostician (with the Verifier's reasoning) while `verify_loops < MAX_VERIFY_LOOPS`.
    - **Loop protection:** a hypothesis whose key `root_cause_service/fault_type` was already rejected is not verified again; the Reporter takes over.
    - A Diagnostician failure goes straight to the Reporter.
37. **The Verifier does not see the specialists' evidence.** It gets only the hypothesis (service, fault type, explanation) and the alert, designs its own falsification checks and runs fresh queries; its window ends "now". This is the separation of hypothesis generation from testing (§1). Its prompt warns that a root cause can still answer `/healthz` quickly, because health checks do not exercise the broken path.
38. **The final result is decided by a pure function** (`reporter.final_decision`, unit-tested), not by an LLM. The result is the Diagnostician's latest top hypothesis, with its confidence adjusted:

    | Outcome | Confidence | Report status |
    |---|---|---|
    | confirmed | +0.1 | complete |
    | inconclusive | ×0.8 | complete |
    | not verified | ×0.8 | partial |
    | rejected (loop ended) | ×0.5 | partial |
    | each failed specialist | ×0.8 | partial |

    The limitations list says why.
39. **The Reporter only writes.** Code fills root cause, fault type, confidence, status and suspect commit; the LLM writes summary, timeline, actions and evidence references.
    - `write_report` is run by the Reporter's code after validation; the Markdown is rendered from the validated object.
    - If the LLM fails, a plain template report is written, so every run ends with a report.
    - A run without any hypothesis gets `root_cause_service: "unknown"` and `fault_type: "no_fault"`: `fault_type` is a fixed enum, and the status `failed` marks it.
40. **Times relative to the alert.** Every tool that returns a timestamp also returns `vs_alert_s` (seconds relative to the alert, negative = before). Examples: deploys, commits, container start/stop, log pattern first/last seen, metric level shifts. "This deploy was 35 s before the alert, that one 2.5 min before" is exactly what separates a guilty change from noise, and LLMs compute it unreliably.
41. **`docker_inspect` sees only the system's components** and refuses `toxiproxy` and `loadgen`. With no argument it returns all components in one call (budget: 4 tool calls). The Docker socket is found via `DOCKER_HOST`, `/var/run/docker.sock` or Docker Desktop's `~/.docker/run/docker.sock`.
42. **`health_check` of an infrastructure component** (postgres, redis, nats) returns the `/healthz` checks of the services that depend on it. These components have no HTTP endpoint, and this is how an engineer would check them.
43. **Git access is read-only by construction:** only `git log` and `git show` run; the commit must match `^[0-9a-f]{4,40}$`, and paths must be relative without `..`.
44. **Two persistence layers.** `data/incidents.db` (our StateStore) has one row per plan, bundle, hypothesis set, verdict and report, as readable JSON for audits and the report. `data/checkpoints.db` is LangGraph's `SqliteSaver`, which checkpoints the graph state after every step (`thread_id` = incident id) for resume and debugging.
45. **`eval/run_scenario.py` accepts several scenario ids** (`s01 s02 …`) and `--repeats`. It writes one JSON per run (`eval/results/<ts>/<scenario>_<n>.json`) with the score, verdicts, call and token counts, and resets the system at the end even if a run crashes.

## 8. Choices made in Phase 4 (reliability & quality)

46. **The run time limit (`RUN_TIMEOUT_S`) is a soft deadline.**
    - It is checked before every LLM call and in both routing functions; a call already in flight may finish.
    - When it has passed, every route leads to the Reporter, which skips its LLM call and writes a template report with status `partial`. The first limitation names the limit.
    - The step limit (`MAX_GRAPH_STEPS`, LangGraph's `recursion_limit`) aborts the graph with `GraphRecursionError`. The Reporter then runs on the last checkpointed state (`graph.get_state`), which is a concrete use of the checkpointer.
    - Either way, a run always ends with a report.
47. **The order service runs database migrations at startup** (`order/migrations/*.sql`, embedded in the binary).
    - They are idempotent (`IF [NOT] EXISTS`) and retried for up to a minute, because the database may still be starting.
    - This lets s09 be a real schema change: the deploy adds `002_drop_unused_orders_index.sql`.
    - `make reset` rebuilds order at `baseline`, whose `001` re-creates the index.
48. **3 million seeded orders.** Postgres scans 300k rows in parallel in 7.6 ms, too fast for a visible slow query. With 3M rows a full scan takes ~75–100 ms, as it would on a production-sized table.
49. **The Postgres healthcheck tests TCP** (`pg_isready -h 127.0.0.1`). On first start, Postgres runs a temporary socket-only server while seeding, and a socket-based check reported "healthy" 11 s too early.
50. **Every shop service is limited to 1 CPU** (`cpus: 1.0`), like a production container limit. Without it, s10's goroutine leak would use all 11 VM cores and slow down *every* service, blurring the root cause.
51. **s11 uses a 5 s latency toxic, not a black-hole "timeout" toxic.** Requests still reach payment, each attempt times out at the gateway's 3 s limit, and the 3 retries make payment's own request rate ~4× normal. The retry storm is visible on both sides, and the fault stays "payment is slow" (latency), which is the ground truth.
52. **s12's load spike is a setup step** (`load_spike`) that starts at injection time. Its baseline therefore has normal traffic, and the spike is what changes.
53. **Diagnostician rules made explicit** (general SRE knowledge, not scenario answers):
    - down vs slow: refused/stopped = dependency_down; timeouts/seconds = latency;
    - a query type that became slow, e.g. after an index change = slow_query of postgres (the deploy's commit still goes into `suspect_commit`);
    - more traffic with no failures and no relevant change = no_fault.
54. **The Metrics Analyst's plan** spends three parallel `zscore_anomaly` calls on latency, calls between services and errors, then its last call on the resources of the most suspicious service (heap, goroutines, query and pool metrics). Leaks and slow queries are invisible in latency-only views.
55. **`LLM_MAX_RPM` was raised from 5 to 15 for `gemini-3.1-flash-lite`.** The 5/min figure came from a different model's 429 error. For this model, 7 back-to-back calls were accepted, so we pace at 15/min. If that is above the real quota, the 429 responses are absorbed by the retry-after logic (and visible in the call log). This roughly halves the evaluation time.
55b. **`RUN_TIMEOUT_S=300` in our `.env` (the default stays 180).** With the free tier, single LLM calls sometimes took 30–49 s (measured in s06, pass 1). That run crossed 180 s and correctly ended as a `partial` template report, with a correct diagnosis. The limit exists to bound runaway runs, not to punish a slow provider, so it is set per environment.
56. **Evaluation design** (`eval/run_all.py`):
    - repeats run round-robin (pass 1 over all scenarios, then pass 2, …), so a drift in the environment does not hit one scenario's repeats only;
    - every run is saved when it finishes, and `--summarize` rebuilds the tables from those files;
    - the "typical run" for the load report is the correct run with the median number of calls, copied to `logs/sample/typical_run.jsonl` with its report in `reports/sample/`.
57. **Deploy patches are generated by `scripts/make_patches.py`** from readable "replace this with that" definitions. Hand-written diffs break as soon as `services/` changes; `tests/test_patches.py` fails when a patch no longer applies.

58. **Tuning after pass 1** (details in `docs/results.md`, "Tuning history"). All fixes are general rules:
    - a 150 s cooldown between evaluation runs;
    - a 5-minute default look-back;
    - "earlier, resolved events are not this incident" in the Log Analyst, Metrics Analyst, Diagnostician and Verifier;
    - the Verifier compares with before via a code-computed PromQL `offset`;
    - the Metrics Analyst's four combined queries;
    - final-decision rules for "everything was rejected";
    - tool arguments in the call log.
59. **Combined PromQL queries** (`A or label_replace(B, …) or …`) let one `zscore_anomaly` call score several metric families for all services. Within the fixed budget of 4 tool calls, the Metrics Analyst can then check latency, dependencies, errors and resources in one parallel turn, instead of choosing two of them and missing a leak (s10, pass 1).

60. **The fault-type taxonomy needs a sense of scale and of the change's kind.** Tuning showed that a weaker model gets the component right but the fault type wrong (pass 2: s09, s11). The prompts now say:
    - schema/index migrations that slow queries are slow_query at postgres;
    - a goroutine leak is ≥10× and growing, and a moderate rise while requests wait is a symptom of latency;
    - the Verifier rejects a hypothesis whose fault type the data contradicts.

61. **The evaluation respects the provider's daily quota.** The free tier of `gemini-3.1-flash-lite` allows 500 requests per day; a full run needs ~15 LLM calls, so 36 runs plus tuning do not fit into one day.
    - When an agent's LLM call fails after all retries for a reason on the provider's side, `run_all.py` does not save the run (that would measure the provider, not the agents) and stops. Provider reasons: quota exhausted, unreachable ("Connection error"), overloaded (503 "high demand"), no answer within `LLM_TIMEOUT_S`.
    - The rule does not look at the outcome: a correct run with a provider failure is excluded too. Excluding only the wrong ones would inflate the accuracy.
    - Found on 27 Oct: the API host was unreachable from the network for a whole evaluation session. Five runs were saved as "wrong" with `unknown / no_fault`, each after one tool call; they were deleted and the guard was extended to connection errors.
    - Later that day Gemini was overloaded (503 "high demand", 60 s timeouts). Three runs had an agent fail after all retries: two wrong (s01 run 2, s11 run 1: the Log and Metrics Analysts got no answer) and one correct (s02 run 2: the Orchestrator failed and the fallback plan worked). All three were moved to `excluded/` in the results directory (kept for audit, not scored) and run again.
    - `--resume <dir>` continues the next day in the same results directory, skipping saved runs.
    - With a paid key the whole evaluation runs in one go.

## 9. Known limitations

- **The Verifier rejects too often.** In the final evaluation it rejected 6 correct hypotheses and no wrong first diagnosis (details in `docs/results.md`). The final-decision rule recovered 5 of the 6; the sixth (s07 run 2) became the only wrong run. Its typical mistake is reading a client-side symptom (a full NATS client buffer, a cache that still serves) as proof that the dependency is up.
- The plan window (15 min before the alert) can still contain the tail of a previous scenario when scenarios run back-to-back. The specialists are told to mark patterns that started long before the alert. Phase 3/4 (the Orchestrator's window, eval cooldowns) will revisit this.
- Loki's `label/values` endpoint can lag a minute behind fresh streams. The Log Analyst uses `error_pattern_summary` / `loki_query` first, and those are not affected.
- One evaluation run takes ~7 min: ~4.5 min of scenario (reset, noise deploys, 120 s warm-up, 90 s observation) plus the investigation (paced by the provider's rate limit). 12 × 3 runs take ~4 h, not the spec's 90 min. Shortening the warm-up would leave the z-score baseline empty, and shortening the observation would leave slow faults (leaks) undeveloped. We chose correct scenarios over a fast evaluation.
