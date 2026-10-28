#!/usr/bin/env bash
# Creates the Cassandra keyspace and table (week 2, step 2). Safe to run again: IF NOT EXISTS.
# Table design and reasons: docs/cassandra.md
set -euo pipefail
cd "$(dirname "$0")/.."

cqlsh() { docker compose exec -T cassandra cqlsh "$@"; }

# Cassandra takes about a minute to start after `make up`; wait until it answers (up to 3 min).
for _ in $(seq 36); do
  if cqlsh -e "SELECT now() FROM system.local" >/dev/null 2>&1; then
    break
  fi
  echo "waiting for Cassandra..."
  sleep 5
done

cqlsh -f /schema/schema.cql
cqlsh -e "DESCRIBE TABLE fraud.txn_by_user"
