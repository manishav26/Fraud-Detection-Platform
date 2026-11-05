#!/usr/bin/env bash
# Creates the cases and audit_log tables and alert-svc's login (week 3, step 5). Safe to run again.
# Postgres's own startup script runs only on an empty disk, and ours isn't, so this is separate.
# Design: docs/alerts.md
set -euo pipefail
cd "$(dirname "$0")/.."

# How to run psql: in the compose container by default; scripts/k8s_up.sh points it at the pod.
PSQL="${PSQL:-docker compose exec -T postgres psql}"
psql() { $PSQL -U fraud -d fraud -v ON_ERROR_STOP=1 "$@"; }

for _ in $(seq 36); do
  if psql -c "SELECT 1" >/dev/null 2>&1; then
    break
  fi
  echo "waiting for Postgres..."
  sleep 5
done

psql -q < deploy/postgres/platform.sql   # sent on stdin, so no file has to exist inside the container
psql -c "\dt"
