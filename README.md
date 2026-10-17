# Incident Responder

A **multi-agent LLM system for root-cause analysis in microservices**. It receives an alert, lets specialist agents investigate logs, metrics and recent changes, combines their evidence into ranked root-cause hypotheses, verifies the top one and writes an incident report.

To evaluate it objectively, the repository also contains:
- a small Go microservice backend (food ordering);
- a full observability stack (Prometheus, Loki, Grafana);
- a fault-injection harness that reproduces incident scenarios with known ground truth.

> Master's course "AI and Machine Learning", Astana IT University. Single source of truth: [SPEC.md](SPEC.md).

![Agent architecture](docs/architecture.png)

| Document | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | interaction scheme, agent loop, backend, anti-leakage boundary |
| [docs/agents.md](docs/agents.md) | agent table: role, inputs, outputs, tools, completion criterion |
| [docs/messages.md](docs/messages.md) | message schemas with JSON examples |
| [docs/scenarios.md](docs/scenarios.md) | test scenarios: fault, alert, expected root cause, symptom check |
| [docs/design-decisions.md](docs/design-decisions.md) | why multi-agent, why these roles, trade-offs, limitations |

**Status:** Phase 4 (reliability & quality) is done.
- All **7 agents** run end to end: Orchestrator → 3 parallel specialists → Diagnostician ⇄ Verifier → Reporter.
- Investigations are persisted (SQLite state store + LangGraph checkpointer) and end with a Markdown/JSON report.
- `eval/run_scenario.py` runs scenarios end to end and scores the result against the ground truth.
- Backend: 5 services with Postgres, Redis and NATS. Harness: all **12 scenarios** (s01–s12), each with its own symptom check.
- Limits: a run-time and a step limit both end in a `partial` report.
- `make eval` runs the full evaluation; results and the per-agent load distribution are in [docs/results.md](docs/results.md).

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
| Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-2.5-flash` |
| OpenRouter | `https://openrouter.ai/api/v1` | `openai/gpt-4o-mini` |
| Ollama (local) | `http://localhost:11434/v1` (`LLM_API_KEY=ollama`) | `qwen2.5:7b` (needs tool-calling support) |

Also set `LLM_API_KEY=...`. The key is read only from the environment or `.env`, which is gitignored; it never appears in code.

**Free tiers:** set `LLM_MAX_RPM` to your requests-per-minute quota. For example, the Gemini free tier allows 5 per minute per model, so use `LLM_MAX_RPM=5`. Calls are then paced instead of failing with 429. One investigation needs about 10–15 LLM calls. `LLM_MODEL=… make investigate …` switches the model for a single run.

For an offline dry run, set `LLM_PROVIDER=fake`. Every agent then returns a deterministic canned answer, marked `[fake]`. The tools still query the live stack. This mode is meant for tests and plumbing checks, not for real diagnosis.

## Demo (3 commands)

```bash
make scenario S=s01                       # ~4.5 min: reset, 2 harmless deploys, warm-up, fault (payment +2 s), symptom check → alerts/s01.json
make investigate ALERT=alerts/s01.json    # the agents investigate the alert
make reset                                # back to the healthy baseline
```

`make investigate` prints a live trace of every agent action. These lines are from a real run with `gemini-3.5-flash`:

```
         planner → window 12:37:06–13:00:30 UTC, focus: gateway, inventory, payment, order, redis, postgres, nats
 metrics_analyst → targets_health() ✓ 3 ms
     log_analyst → error_pattern_summary(start=…, end=…) ✓ 331 ms
 metrics_analyst → zscore_anomaly(promql=histogram_quantile(0.95, sum by (le, service, route) (rate(http_request_duration_se…) ✓ 10 ms
     log_analyst → loki_query(logql={service="payment"} | json, …) ✓ 24 ms
 metrics_analyst → done: 3 evidence item(s), status=ok
     log_analyst → waited 55 s (LLM_MAX_RPM=5)
     log_analyst → done: 1 evidence item(s), status=ok
   diagnostician → dependency_graph() ✓ 0 ms
   diagnostician → top hypothesis: payment / latency (confidence 0.80)
```

It then prints:
- the evidence from each specialist;
- the ranked root-cause hypotheses;
- the Verifier's verdict with the checks it ran;
- the final incident report: root cause, confidence, summary, recommended actions, and the path of `reports/<incident_id>.md`.

Expected result for s01: **root cause `payment`, fault type `latency`**. The alert only says that the *gateway* is slow.

Scenarios s01–s08 are described in [docs/scenarios.md](docs/scenarios.md). The harness CLI (`uv run --project agents python -m chaos …`):

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
| Call log: one JSON line per LLM call and per tool call (agent, latency, status, attempt, tokens) | `logs/run_<run_id>.jsonl` |
| Final state of a run (plan, evidence bundles, hypotheses, failures) | `logs/run_<run_id>_result.json` |
| Committed sample run (for grading) | `logs/sample/` |
| Generated alerts | `alerts/` |
| Deploy log (what the Change Investigator will read) | `data/deploy_events.jsonl`, git history of `target-repo/` |
| Incident reports (Markdown + JSON) | `reports/<incident_id>.md` / `.json` |
| Task state of every investigation (plan, evidence, hypotheses, verdicts, report) | `data/incidents.db` (SQLite) |
| LangGraph checkpoints (graph state after each step) | `data/checkpoints.db` |
| Evaluation runs | `eval/results/<timestamp>/` |

The CLI also prints each agent's share of all calls. The requirement is that no agent exceeds 40%.

## Tests

```bash
make test        # pytest (schemas, tool logic, anti-leakage, graph with fake LLM) + go test
```

The graph tests run the real tool code against a mocked Prometheus/Loki API with the fake LLM. They check:
- routing and the structured hand-off;
- budgets and the repair call;
- retries and graceful degradation;
- that every call is logged.

## Evaluation

```bash
make eval-scenario S="s01 s02 s03" REPEATS=1   # = uv run --project agents python eval/run_scenario.py s01 s02 s03
```

For each scenario and repeat this runs the whole chain:
1. reset, noise deploys, warm-up, fault, alert;
2. all 7 agents investigate;
3. the result is scored against the scenario's ground truth.

Each run is saved to `eval/results/<timestamp>/<scenario>_<n>.json`, a summary table is printed, and the system is reset at the end. A run is correct when the reported root-cause service and fault type match the scenario (SPEC §8). For deploy scenarios the suspect commit is scored too.

```bash
make eval                                   # all 12 scenarios x 3 repeats -> docs/results.md (~4 h at 15 LLM calls/min)
make load-report RUN=logs/run_<id>.jsonl    # per-agent share of calls in one run (must be <= 40%)
uv run --project agents python eval/run_all.py --summarize eval/results/<ts>   # rebuild the tables from saved runs
```

`make eval` also copies a typical correct run to `logs/sample/typical_run.jsonl` (with its report in `reports/sample/`) and writes its load table into `docs/results.md`.

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
tests/           harness tests (scenario files, patches apply)
agents/          Python package `responder` (uv project)
  responder/     schemas, llm, runtime, calllog, graph, cli, agents/ (+ prompts/), tools/
  tests/         pytest
docs/            architecture, agents, messages, design decisions
```

## Troubleshooting

| Problem | Fix |
|---|---|
| `make up` fails with "port is already allocated" | Something else uses 8080–8084, 3000, 3100, 9090 or 8474. Stop it (`lsof -i :8080`) or change the host port in `docker-compose.yml`. |
| Containers restart or are OOM-killed | Give Docker ≥ 6 GB of memory (Docker Desktop → Settings → Resources). |
| `target-repo/ is missing` | Run `make target-init` once. |
| `LLM_API_KEY is not set` | `cp .env.example .env` and fill in the LLM section, or use `LLM_PROVIDER=fake`. |
| `429 … exceeded your current quota` | Set `LLM_MAX_RPM` to your plan's per-minute limit (Gemini free tier: 5). If the *daily* quota is used up, switch `LLM_MODEL`: quotas are per model. |
| `Connection error` / `Connection reset by peer` for every LLM call | Your network blocks the provider's API (check `curl -I https://generativelanguage.googleapis.com`). Switch network or VPN, or use a provider you can reach (e.g. OpenRouter). The run still ends with a `failed` report: that is the degradation path working. |
| Reports say "stopped early: run time limit reached" | The provider's rate limit slows the run. Raise `RUN_TIMEOUT_S` in `.env` (e.g. 300) or `LLM_MAX_RPM` if your plan allows more calls per minute. |
| `make eval` stops with "the LLM provider's quota is exhausted" | Free tiers also cap requests per DAY (`gemini-3.1-flash-lite`: 500/day; one run needs ~15). The interrupted run is not saved; continue the next day with `uv run --project agents python eval/run_all.py --repeats 3 --resume eval/results/<ts>`. |
| `503 … model is currently experiencing high demand` | The provider is overloaded (temporary). Retry later, or use another model, e.g. `LLM_MODEL=gemini-3.5-flash make investigate ALERT=…`. |
| `400 … missing a thought_signature` | Fixed: the runtime sends Gemini's assistant messages back unchanged. If you see it, you are on an old checkout. |
| A symptom check prints ✗ | The fault did not fully develop (e.g. Docker was very slow). Run the scenario again; `make reset` first if anything looks stuck. |
| Metrics evidence says "series did not exist in the baseline" | The stack was started very recently. `make scenario` waits for a warm-up, but give it a couple of minutes of normal traffic first. |
| No logs in Grafana right after `make up` | Loki needs ~15–30 s. `make up` waits for Loki to be ready. |
| Start from scratch | `make down && rm -rf target-repo && make target-init && make up` |
