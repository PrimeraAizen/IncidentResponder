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

## 6. Known limitations (Phase 1)

- The prototype has no Change Investigator, Verifier or Reporter yet. A bad deploy cannot be identified until Phase 3.
- `make scenario` relies on the always-on load generator. The scenario's `load:` block (scenario-specific RPS) is applied by the eval harness from Phase 3 on.
- Loki's `label/values` endpoint can lag a minute behind fresh streams. The Log Analyst uses `error_pattern_summary` / `loki_query` first, and those are not affected.
- The topology lists all eight components, including those that arrive in Phase 2.
