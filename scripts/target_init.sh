#!/usr/bin/env bash
# Creates target-repo/: a separate git repository holding a copy of services/.
#
# Compose builds the shop images from it, and the deploy simulator commits to it,
# so "bad deploy" incidents leave a real git history for the Change Investigator.
# The first commit is tagged `baseline`; `make reset` returns to it.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="$ROOT/target-repo"

if [ -d "$TARGET/.git" ]; then
  echo "target-repo already exists (HEAD $(git -C "$TARGET" rev-parse --short HEAD)). Delete it to re-create."
  exit 0
fi

mkdir -p "$TARGET"
cp -R "$ROOT/services/." "$TARGET/"

git -C "$TARGET" init -q -b main
git -C "$TARGET" add -A
# A fixed team identity: commits in this repo play the role of a real team's history.
git -C "$TARGET" -c user.name="Shop Platform Team" -c user.email="platform@shop.example" \
  -c commit.gpgsign=false commit -q -m "Import shop services (gateway, order, payment)"
git -C "$TARGET" tag baseline

echo "target-repo created at $(git -C "$TARGET" rev-parse --short HEAD), tagged 'baseline'"
