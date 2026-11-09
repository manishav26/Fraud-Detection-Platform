#!/usr/bin/env bash
# Creates the Cassandra keyspace and table (week 2, step 2) and its logins (week 4, step 3).
# Safe to run again. Table design and reasons: docs/cassandra.md
#
# Logins (Cassandra checks them since week 4; dev passwords, like Postgres's):
#   fraud_admin  superuser: this script and the laptop tools (backfill, read timing, parity)
#   scoring      scoring-svc only: read and write fraud.txn_by_user, nothing else
# Cassandra's built-in `cassandra` superuser has a password everyone knows, so after
# fraud_admin exists it gets a random password and can no longer log in.
set -euo pipefail
cd "$(dirname "$0")/.."

# How to run cqlsh: in the compose container by default; scripts/k8s_up.sh points it at the pod.
CQLSH="${CQLSH:-docker compose exec -T cassandra cqlsh}"
ADMIN_USER=fraud_admin
ADMIN_PASSWORD=fraud_admin_dev_only
SCORING_PASSWORD=scoring_dev_only

as() { local user=$1 password=$2; shift 2; $CQLSH -u "$user" -p "$password" "$@"; }

# Cassandra takes about a minute to start, and creates its default login a few seconds after it
# answers. Wait until one of the two logins works (up to 3 min).
LOGIN=""
for _ in $(seq 36); do
  if as "$ADMIN_USER" "$ADMIN_PASSWORD" -e "SELECT now() FROM system.local" >/dev/null 2>&1; then
    LOGIN=admin; break
  fi
  if as cassandra cassandra -e "SELECT now() FROM system.local" >/dev/null 2>&1; then
    LOGIN=default; break
  fi
  echo "waiting for Cassandra..."
  sleep 5
done
[ -n "$LOGIN" ] || { echo "Cassandra did not accept a login" >&2; exit 1; }

if [ "$LOGIN" = default ]; then
  echo "first run with logins on: creating $ADMIN_USER, then locking the default login"
  as cassandra cassandra -e "CREATE ROLE IF NOT EXISTS $ADMIN_USER
    WITH PASSWORD = '$ADMIN_PASSWORD' AND SUPERUSER = true AND LOGIN = true"
  RANDOM_PASSWORD=$(openssl rand -hex 16)   # never stored: nobody needs this login again
  as "$ADMIN_USER" "$ADMIN_PASSWORD" -e \
    "ALTER ROLE cassandra WITH PASSWORD = '$RANDOM_PASSWORD' AND SUPERUSER = false AND LOGIN = false"
fi

admin() { as "$ADMIN_USER" "$ADMIN_PASSWORD" "$@"; }
admin < deploy/cassandra/schema.cql   # on stdin, so no file has to exist inside the container

# scoring may read and write its one table: no other table, no schema changes, no logins.
# (Cassandra's MODIFY also allows DELETE and TRUNCATE on that table; it has no finer permission.)
admin -e "CREATE ROLE IF NOT EXISTS scoring WITH PASSWORD = '$SCORING_PASSWORD' AND LOGIN = true"
admin -e "GRANT SELECT ON fraud.txn_by_user TO scoring"
admin -e "GRANT MODIFY ON fraud.txn_by_user TO scoring"
admin -e "LIST ALL PERMISSIONS OF scoring"
admin -e "DESCRIBE TABLE fraud.txn_by_user"
