# Incident Responder

A **multi-agent LLM system for root-cause analysis in microservices**. It receives an alert, lets specialist agents investigate logs, metrics and recent changes, combines their evidence into ranked root-cause hypotheses, verifies the top one and writes an incident report. The agents only recommend actions; they never change the system.

To evaluate it objectively, the repository also contains:
- a small Go microservice backend (food ordering: 5 services with Postgres, Redis and NATS);
- a full observability stack (Prometheus, Loki, Grafana);
- a fault-injection harness that reproduces 12 incident scenarios with known ground truth.

> Master's course "AI and Machine Learning", Astana IT University. Single source of truth: [SPEC.md](SPEC.md).

## Results

Full evaluation: 12 scenarios × 3 repeats with `gemini-3.1-flash-lite` (free tier), 17–27 Oct 2026. Details are in [docs/results.md](docs/results.md).

| Measure | Result |
|---|---|
| Scenarios correct (majority of 3 repeats) | **12/12** |
| Runs correct (root-cause service and fault type) | **35/36 (97%)** |
| Suspect commit named, in the 4 deploy scenarios | **12/12** |
| Largest share of all LLM + tool calls by one agent (typical run) | **21%** (limit: 40%) |
| Mean investigation time / tokens per run | 64 s / ~40,000 |

The one wrong run (s07, repeat 2) named the right component (NATS) with the wrong fault type. The Verifier had rejected the correct first hypothesis. [docs/results.md](docs/results.md) analyzes all 40 Verifier verdicts.

## How it works

![Agent architecture](docs/architecture.png)

| Agent | Role |
|---|---|
| Orchestrator | Turns the alert into a plan: time window, focus services, one task per specialist. No analysis, no tools. |
| Log Analyst | Error patterns in Loki: what, where, since when. |
| Metrics Analyst | Anomalies in Prometheus, compared with the baseline before the alert. |
| Change Investigator | Deploys, commits, diffs and container restarts near the alert. |
| Diagnostician | Combines all evidence into up to 3 ranked root-cause hypotheses. |
| Verifier | Tests the top hypothesis with fresh queries against the baseline: confirmed or rejected. |
| Reporter | Final report: root cause, confidence, timeline, evidence, recommended actions. |

The three specialists run in parallel. A rejection sends the Verifier's reasons back to the Diagnostician, at most twice. Agents exchange Pydantic models (JSON). Every LLM and tool call is logged, and the state of every investigation is stored in SQLite.

| Document | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | interaction scheme, agent loop, backend, anti-leakage boundary |
| [docs/agents.md](docs/agents.md) | agent table: role, inputs, outputs, tools, completion criterion |
| [docs/messages.md](docs/messages.md) | message schemas with JSON examples |
| [docs/scenarios.md](docs/scenarios.md) | the 12 test scenarios: fault, alert, expected root cause, symptom check |
| [docs/results.md](docs/results.md) | evaluation, load distribution, Verifier analysis, tuning history |
| [docs/design-decisions.md](docs/design-decisions.md) | why multi-agent, why these roles, trade-offs, known limitations |

---

## Prerequisites

- **Docker** Desktop or Engine with Compose v2. Give Docker **≥ 6 GB of memory**.
- **uv** (Python package manager): `brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`. uv downloads Python 3.12 by itself.
- `make`, `git`, `curl`.
- Go ≥ 1.22 only for `make test`. The services are built inside Docker.
- An **OpenAI-compatible LLM endpoint**. Without one, you can still run everything with `LLM_PROVIDER=fake` (canned answers).

## Setup

```bash
cp .env.example .env        # then edit the LLM section (see below)
make target-init            # creates target-repo/ (a separate git repo the services are built from)
make up                     # builds and starts everything, waits until healthy (~1 min the first time)
```

Then open Grafana at http://localhost:3000 (dashboard "Shop — service overview"). Prometheus is at http://localhost:9090.

### Choosing an LLM provider

Any OpenAI-compatible endpoint works. Set three variables in `.env`:

| Provider | `LLM_BASE_URL` | `LLM_MODEL` (example) |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-3.1-flash-lite` (the evaluated model) |
| OpenRouter | `https://openrouter.ai/api/v1` | `openai/gpt-4o-mini` |
| Ollama (local) | `http://localhost:11434/v1` (`LLM_API_KEY=ollama`) | `gpt-oss:20b` (must support tool calling) |

Also set `LLM_API_KEY=...`. The key is read only from the environment or `.env`, which is gitignored; it never appears in code.

**Free tiers.** The evaluation ran on Gemini's free tier with these settings in `.env`:

```bash
LLM_MODEL=gemini-3.1-flash-lite
LLM_MAX_RPM=15        # client-side pacing: calls wait instead of failing with 429
RUN_TIMEOUT_S=300     # single free-tier calls sometimes take 30–50 s
```

That model allows 500 requests per day. One investigation needs about 15 LLM calls, so a day covers about 30 investigations. `LLM_MODEL=… make investigate …` switches the model for a single run.

**Local models (Ollama).**
- For reasoning models such as gpt-oss, add `LLM_REASONING_EFFORT=low`: fewer thinking tokens, faster answers.
- Ollama can run on another machine in your network. Set `OLLAMA_HOST=0.0.0.0:11434` there, allow port 11434 in its firewall, and use `LLM_BASE_URL=http://<its-ip>:11434/v1` here.
- Measured: `gpt-oss:20b` on a laptop with a GTX 1650 (4 GB VRAM, so mostly on the CPU) diagnosed s01 correctly, but one investigation took about 10 min. On such hardware set `LLM_TIMEOUT_S=600` and `RUN_TIMEOUT_S=1800`.

**Offline dry run.** Set `LLM_PROVIDER=fake`. Every agent then returns a deterministic canned answer, marked `[fake]`. The tools still query the live stack. This mode is meant for tests and plumbing checks, not for real diagnosis.

## Demo (3 commands)

```bash
make scenario S=s01                       # ~4.5 min: reset, 2 harmless deploys, warm-up, fault (payment +2 s), symptom check → alerts/s01.json
make investigate ALERT=alerts/s01.json    # the agents investigate the alert
make reset                                # back to the healthy baseline
```

`make investigate` prints a live trace of every agent action. This is a real run of the final system with `gemini-3.1-flash-lite` (29 Oct 2026, about 2 min). Long lines are cut with `…`:

```
LLM: gemini-3.1-flash-lite   alert started at 2026-10-29 13:12:41 UTC

    orchestrator → plan: window 13:07:41–13:13:42 UTC, focus: gateway, inventory, payment, order, postgres, redis, nats
change_investigator → task: Identify any deployments, configuration changes, or container restarts/OOM kills across …
     log_analyst → task: Search for error logs, timeout exceptions, or slow query warnings in gateway, inventory, …
 metrics_analyst → task: Compare p95 latency, request rates, and resource saturation (DB connection pools, Redis …
 metrics_analyst → zscore_anomaly(promql=histogram_quantile(0.95, sum by (le, service, route) (rate(http_request_duration_se…) ✓ 12 ms
 metrics_analyst → zscore_anomaly(promql=histogram_quantile(0.95, sum by (le, service, target) (rate(upstream_request_durati…) ✓ 9 ms
 metrics_analyst → zscore_anomaly(promql=sum by (service) (rate(http_requests_total{status=~"5.."}[1m])) / sum by (service) …) ✓ 6 ms
 metrics_analyst → zscore_anomaly(promql=label_replace(sum by (service) (go_goroutines), "metric", "goroutines", "", "") or …) ✓ 7 ms
     log_analyst → error_pattern_summary(start=2026-10-29T13:07:41.588003Z, end=2026-10-29T13:13:42.837190Z) ✓ 355 ms
 metrics_analyst → done: 5 evidence item(s), status=ok
     log_analyst → loki_query(logql={service="payment"} | json | level="ERROR" or level="WARN", limit=100) ✓ 10 ms
     log_analyst → done: 1 evidence item(s), status=ok
change_investigator → deploy_events(start=2026-10-29T13:07:41.588003Z, end=2026-10-29T13:13:42.837190Z) ✓ 1 ms
change_investigator → docker_inspect() ✓ 102 ms
change_investigator → git_diff(commit=f78fc57) ✓ 33 ms
change_investigator → done: 2 evidence item(s), status=ok
   diagnostician → combining 8 evidence item(s) from 3 bundle(s)
   diagnostician → dependency_graph() ✓ 0 ms
   diagnostician → top hypothesis: payment / latency (confidence 0.90)
        verifier → testing: payment / latency
        verifier → prom_query(promql=histogram_quantile(0.95, sum by (le, target) (rate(upstream_request_duration_second…) ✓ 7 ms
        verifier → prom_query(promql=go_goroutines{service="payment"} / go_goroutines{service="payment"} offset 5m) ✓ 4 ms
        verifier → verdict: confirmed (2 check(s))
        reporter → report complete: payment / latency (confidence 1.00) → reports/inc-20261029-131342-17f0.md
```

It then prints tables of the evidence and the ranked hypotheses, the Verifier's checks, and the report:

```
Root cause: payment / latency   confidence 1.00
Users experienced high latency on the /api/orders endpoint due to slow upstream responses from the payment service. …
  → Implement circuit breakers on the gateway for the payment service to prevent cascading latency.
  → Add timeout configurations for calls to the external payment provider.
  → Set up alerts for high goroutine counts in the payment service.
  → Conduct a load test to determine the payment service's capacity under high external provider latency.

Calls (llm+tool): change_investigator 3+3 (21%), diagnostician 2+1 (11%), log_analyst 3+2 (18%), metrics_analyst 2+4 (21%),
orchestrator 1+0 (4%), reporter 1+1 (7%), verifier 3+2 (18%)
```

Expected result for s01: **root cause `payment`, fault type `latency`**. The alert only says that the *gateway* is slow. Note what the agents did *not* do: the Change Investigator found a deploy of inventory 151 s before the alert (a harmless cache-TTL change, part of the scenario's noise), and the Diagnostician did not blame it.

All 12 scenarios are described in [docs/scenarios.md](docs/scenarios.md). The harness CLI (`uv run --project agents python -m chaos …`):

| Command | What it does |
|---|---|
| `list` | list scenarios |
| `scenario s04` | full run: reset → noise deploys → warm-up → fault → symptom check → alert file |
| `apply s04` | inject a fault immediately (no reset, no waiting) |
| `deploy order s04_order_promotions.patch` | simulate a release: patch → commit → rebuild → `data/deploy_events.jsonl` |
| `reset` | same as `make reset` |

## Where the output goes

| What | Where |
|---|---|
| Call log: one JSON line per LLM call and per tool call (agent, arguments, latency, status, attempt, tokens) | `logs/run_<run_id>.jsonl` |
| Final state of a run (plan, evidence bundles, hypotheses, failures) | `logs/run_<run_id>_result.json` |
| Committed typical run (proof of the load distribution) | `logs/sample/typical_run.jsonl` |
| Generated alerts | `alerts/` |
| Deploy log (what the Change Investigator reads) | `data/deploy_events.jsonl`, git history of `target-repo/` |
| Incident reports (Markdown + JSON) | `reports/<incident_id>.md` / `.json`; a committed example in `reports/sample/` |
| Task state of every investigation (plan, evidence, hypotheses, verdicts, report) | `data/incidents.db` (SQLite) |
| LangGraph checkpoints (graph state after each step) | `data/checkpoints.db` |
| Evaluation runs (one JSON file per run) | `eval/results/<timestamp>/` |

The CLI also prints each agent's share of all calls. The requirement is that no agent exceeds 40%.

## Tests

```bash
make test        # pytest (schemas, tool logic, anti-leakage, graph with fake LLM, scoring) + go test
```

The graph tests run the real tool code against a mocked Prometheus/Loki API with the fake LLM. They check:
- routing and the structured hand-off;
- budgets and the repair call;
- retries and graceful degradation;
- that every call is logged.

## Evaluation

```bash
make eval-scenario S="s01 s02 s03" REPEATS=1   # some scenarios (= uv run --project agents python eval/run_scenario.py s01 s02 s03)
make eval                                      # all 12 scenarios × 3 repeats → docs/results.md
```

For each scenario and repeat this runs the whole chain:
1. reset, noise deploys, warm-up, fault, alert;
2. all 7 agents investigate;
3. the result is scored against the scenario's ground truth.

A run is correct when the reported root-cause service and fault type match the scenario, and no service the scenario forbids is blamed (SPEC §8). For deploy scenarios the suspect commit is scored too. Each run is saved to `eval/results/<timestamp>/<scenario>_<n>.json` as soon as it finishes. At the end, `make eval` writes the tables into [docs/results.md](docs/results.md) and copies a typical correct run to `logs/sample/` and `reports/sample/`.

**Time and quota.** One run takes about 8 min: ~7 min of scenario (a 150 s cooldown, so the previous fault leaves the look-back window, then reset, deploys, warm-up and observation) plus the investigation. The full evaluation takes about 5 h and ~540 LLM requests, more than one day of a free tier. It can be done in parts:

```bash
# continue an interrupted evaluation: saved runs are skipped; --max-runs N does only N runs now
uv run --project agents python eval/run_all.py --repeats 3 --resume eval/results/<ts> --max-runs 5
# rebuild the tables from the saved runs
uv run --project agents python eval/run_all.py --summarize eval/results/<ts>
# per-agent share of calls in one run (must be <= 40%)
make load-report RUN=logs/run_<id>.jsonl
```

If an agent's LLM call fails after all retries because of the *provider*, the run is not saved and the evaluation stops. Provider failures are: quota used up, unreachable, overloaded (503), no answer in time. Such a run would measure the provider, not the agents (design decision 61). Resume later with `--resume`.

## Where each requirement is proven

| Requirement | Evidence |
|---|---|
| 7 agents, one role each, no overlap | [docs/agents.md](docs/agents.md), `agents/responder/agents/` |
| ≤ 5 tools per agent | `agents/tests/test_tools_registry.py`, [docs/agents.md](docs/agents.md) |
| No agent > 40% of calls | `logs/sample/typical_run.jsonl` + load table in [docs/results.md](docs/results.md) |
| Orchestrator only plans | 1 LLM call, 0 tools: `agents/responder/agents/orchestrator.py` and its prompt |
| Interaction diagram | [docs/architecture.md](docs/architecture.md) |
| Structured messages | `agents/responder/schemas.py`, [docs/messages.md](docs/messages.md) |
| ≥ 2 external tools / data sources | Prometheus, Loki, git, Docker API, deploy log: `agents/responder/tools/` |
| Explicit task state | `agents/responder/state_store.py` → `data/incidents.db`, LangGraph checkpointer |
| Step and time limits, loop protection | `MAX_GRAPH_STEPS`, `RUN_TIMEOUT_S`, `MAX_VERIFY_LOOPS`: `agents/responder/graph.py`, `runtime.py` |
| Retries and clear failure messages | `agents/responder/llm.py`, `runtime.py`, tests |
| Every call logged | `agents/responder/calllog.py` → `logs/` |
| No keys in code | `.env.example`, `.gitignore` |
| Test scenarios with expected results | `scenarios/`, [docs/scenarios.md](docs/scenarios.md) |
| Verifier agent | `agents/responder/agents/verifier.py` |

## All make targets

```
make target-init   create target-repo/ (first time only)
make up            build + start everything, wait until healthy
make down          stop everything and delete its data
make reset         back to the healthy baseline (clear faults, reset target-repo)
make scenario      inject a scenario and write its alert: make scenario S=s01
make investigate   run the agents on an alert: make investigate ALERT=alerts/s01.json
make test          unit tests (pytest: agents + harness; go test)
make eval-scenario evaluate some scenarios: make eval-scenario S="s01 s02" REPEATS=1
make eval          all scenarios x 3 repeats, results into docs/results.md
make load-report   per-agent share of calls of one run: make load-report RUN=logs/run_xxx.jsonl
```

## Repository layout

```
services/        Go services (gateway, order, payment, inventory, notification) + pkg/obs
target-repo/     (gitignored) git repo copied from services/; compose builds from here
infra/           Prometheus (+ alert rules), Loki, Alloy, Grafana (datasources + dashboard), Toxiproxy (+ baseline DB latency), Postgres seed
loadgen/         Go load generator (always-on background traffic)
chaos/           harness CLI: faults (Toxiproxy, docker stop, /admin/chaos), deploy simulator, patches/
scenarios/       sNN_*.yaml: fault + alert + expected root cause + symptom check
agents/          Python package `responder` (uv project)
  responder/     schemas, llm, runtime, calllog, state_store, graph, cli, agents/ (+ prompts/), tools/
  tests/         pytest
eval/            run_scenario.py, run_all.py, scoring.py, load_report.py; results/ (gitignored)
tests/           harness and eval tests (scenario files, patches apply, scoring, summaries)
scripts/         target-repo init, readiness wait, Toxiproxy baseline, patch and dashboard generators
docs/            architecture, agents, messages, scenarios, results, design decisions
logs/ reports/   call logs and incident reports (only the sample/ folders are committed)
data/ alerts/    state store, checkpoints, deploy log; generated alerts (gitignored)
```

## Troubleshooting

| Problem | Fix |
|---|---|
| `make up` fails with "port is already allocated" | Something else uses 8080–8084, 3000, 3100, 9090 or 8474. Stop it (`lsof -i :8080`) or change the host port in `docker-compose.yml`. |
| Containers restart or are OOM-killed | Give Docker ≥ 6 GB of memory (Docker Desktop → Settings → Resources). |
| `target-repo/ is missing` | Run `make target-init` once. |
| `LLM_API_KEY is not set` | `cp .env.example .env` and fill in the LLM section, or use `LLM_PROVIDER=fake`. |
| `429 … exceeded your current quota` | Set `LLM_MAX_RPM` to your plan's per-minute limit (we use 15 for `gemini-3.1-flash-lite`). If the *daily* quota is used up, wait for the reset or switch `LLM_MODEL`: quotas are per model. |
| `Connection error` / `Connection reset by peer` for every LLM call | Your network blocks the provider's API. Check with `curl -s -o /dev/null -w "%{http_code}\n" https://generativelanguage.googleapis.com/`: `404` = reachable, `000` = blocked. Switch network or VPN, or use a provider you can reach. `make investigate` still ends with a `failed` report: that is the degradation path working. |
| `503 … model is currently experiencing high demand` | The provider is overloaded (temporary). Calls are retried; if it persists, try again later or another model. |
| `make eval` stops with "✗ … this run is not saved" | The provider failed (quota, network or overload; see the rows above). Fix the cause or wait, then continue with `eval/run_all.py --repeats 3 --resume eval/results/<ts>`. |
| Reports say "stopped early: run time limit reached" | The provider is slow or rate-limited. Raise `RUN_TIMEOUT_S` in `.env` (e.g. 300), or `LLM_MAX_RPM` if your plan allows more calls per minute. |
| `400 … missing a thought_signature` | Fixed: the runtime sends Gemini's assistant messages back unchanged. If you see it, you are on an old checkout. |
| A symptom check prints ✗ | The fault did not fully develop (e.g. Docker was very slow). Run the scenario again; `make reset` first if anything looks stuck. |
| Metrics evidence says "series did not exist in the baseline" | The stack was started very recently. `make scenario` waits for a warm-up, but give it a couple of minutes of normal traffic first. |
| No logs in Grafana right after `make up` | Loki needs ~15–30 s. `make up` waits for Loki to be ready. |
| Start from scratch | `make down && rm -rf target-repo && make target-init && make up` |
