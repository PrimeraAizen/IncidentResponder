# Incident Responder: entry points. `make help` lists them.
SHELL := /bin/bash

UV := uv run --project agents
# The target repo HEAD becomes the COMMIT build arg of the service images (build_info metric).
COMPOSE := TARGET_COMMIT=$$(git -C target-repo rev-parse --short HEAD 2>/dev/null || echo unknown) docker compose

S ?= s01
ALERT ?= alerts/$(S).json
REPEATS ?= 1

.PHONY: help target-init up down reset scenario investigate eval-scenario eval load-report test

help: ## show this help
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-13s %s\n", $$1, $$2}'

target-init: ## create target-repo/ (first time only)
	@./scripts/target_init.sh

up: ## build + start everything, wait until healthy
	@test -d target-repo/.git || { echo "target-repo/ is missing: run 'make target-init' first"; exit 1; }
	$(COMPOSE) up -d --build --wait --wait-timeout 180
	@./scripts/wait_ready.sh
	@./scripts/toxiproxy_baseline.sh

down: ## stop everything and delete its data
	$(COMPOSE) down -v --remove-orphans

reset: ## back to the healthy baseline (clear faults, reset target-repo, clear deploy log)
	$(UV) python -m chaos reset

scenario: ## run a scenario (reset, noise deploys, warm-up, fault, symptom check, alert): make scenario S=s01
	$(UV) python -m chaos scenario $(S)

investigate: ## run the agents on an alert: make investigate ALERT=alerts/s01.json
	$(UV) responder investigate --alert $(ALERT)

eval-scenario: ## evaluate the agents end to end: make eval-scenario S="s01 s02" REPEATS=1
	$(UV) python eval/run_scenario.py $(S) --repeats $(REPEATS)

eval: ## run all scenarios and score them (Phase 4)
	@echo "eval/run_all.py arrives in Phase 4"; exit 1

load-report: ## per-agent share of calls: make load-report RUN=logs/run_xxx.jsonl (Phase 4)
	@echo "eval/load_report.py arrives in Phase 4"; exit 1

test: ## unit tests (pytest + go test)
	$(UV) pytest -q
	cd services && go test ./...
