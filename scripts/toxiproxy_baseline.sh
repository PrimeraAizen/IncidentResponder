#!/usr/bin/env bash
# Applies the always-on baseline toxics (infra/toxiproxy/baseline/*.json) - see the README there.
# Idempotent: an already existing toxic (HTTP 409) is fine.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
API="${TOXIPROXY_URL:-http://localhost:8474}"

for file in "$ROOT"/infra/toxiproxy/baseline/*.json; do
  proxy="$(basename "$file" .json)"
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/proxies/$proxy/toxics" \
         -H 'Content-Type: application/json' --data @"$file")
  case "$code" in
    200|409) echo "✓ baseline toxic on $proxy" ;;
    *) echo "✗ could not add baseline toxic on $proxy (HTTP $code)"; exit 1 ;;
  esac
done
