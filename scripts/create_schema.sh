#!/usr/bin/env bash
# Creates the Cassandra keyspace and table (week 2, step 2). Safe to run again: IF NOT EXISTS.
# Table design and reasons: docs/cassandra.md
set -euo pipefail
cd "$(dirname "$0")/.."

# How to run cqlsh: in the compose container by default; scripts/k8s_up.sh points it at the pod.
CQLSH="${CQLSH:-docker compose exec -T cassandra cqlsh}"
cqlsh() { $CQLSH "$@"; }

# Cassandra takes about a minute to start; wait until it answers (up to 3 min).
for _ in $(seq 36); do
  if cqlsh -e "SELECT now() FROM system.local" >/dev/null 2>&1; then
    break
  fi
  echo "waiting for Cassandra..."
  sleep 5
done

cqlsh < deploy/cassandra/schema.cql   # sent on stdin, so no file has to exist inside the container
cqlsh -e "DESCRIBE TABLE fraud.txn_by_user"
