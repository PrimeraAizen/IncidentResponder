#!/usr/bin/env bash
# Waits until the gateway and the observability stack answer (used by `make up`).
# Loki needs ~15 s after start before /ready returns 200.
set -euo pipefail

TIMEOUT="${WAIT_TIMEOUT:-180}"
deadline=$((SECONDS + TIMEOUT))

for check in \
  "gateway=http://localhost:8080/healthz" \
  "prometheus=http://localhost:9090/-/ready" \
  "loki=http://localhost:3100/ready" \
  "grafana=http://localhost:3000/api/health"; do
  name="${check%%=*}"
  url="${check#*=}"
  until curl -fsS -m 2 "$url" >/dev/null 2>&1; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "✗ $name is not ready at $url after ${TIMEOUT}s (see: docker compose logs $name)"
      exit 1
    fi
    sleep 2
  done
  echo "✓ $name ready"
done
echo "All up. Grafana: http://localhost:3000  Prometheus: http://localhost:9090"
