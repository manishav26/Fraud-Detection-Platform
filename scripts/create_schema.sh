#!/usr/bin/env bash
# Creates the Cassandra keyspace and table (week 2, step 2) and its logins (week 4, step 3).
# Safe to run again. Table design and reasons: docs/cassandra.md
#
# Logins (Cassandra checks them since week 4):
#   fraud_admin  superuser: this script and the laptop tools (backfill, read timing, parity)
#   scoring      scoring-svc only: read and write fraud.txn_by_user, nothing else
#   features     the feature job only: write features_by_txn and user_features, nothing else
# Cassandra's built-in `cassandra` superuser has a password everyone knows, so after
# fraud_admin exists it gets a random password and can no longer log in.
#
# Passwords: compose uses the dev ones below. In the cluster, scripts/k8s_up.sh passes the
# encrypted ones (CASSANDRA_ADMIN_PASSWORD, CASSANDRA_SCORING_PASSWORD; docs/secrets.md) and this
# script changes both logins to match, so running it again after `make k8s-secrets ROTATE=1`
# rotates them.
set -euo pipefail
cd "$(dirname "$0")/.."

# How to run cqlsh: in the compose container by default; scripts/k8s_up.sh points it at the pod.
CQLSH="${CQLSH:-docker compose exec -T cassandra cqlsh}"
ADMIN_USER=fraud_admin
DEV_ADMIN_PASSWORD=fraud_admin_dev_only   # compose; and a cluster from before the encrypted ones
ADMIN_PASSWORD=${CASSANDRA_ADMIN_PASSWORD:-$DEV_ADMIN_PASSWORD}
SCORING_PASSWORD=${CASSANDRA_SCORING_PASSWORD:-scoring_dev_only}
FEATURES_PASSWORD=${CASSANDRA_FEATURES_PASSWORD:-features_dev_only}
PREVIOUS_ADMIN_PASSWORD=${PREVIOUS_CASSANDRA_ADMIN_PASSWORD:-$DEV_ADMIN_PASSWORD}   # before a rotation

as() { local user=$1 password=$2; shift 2; $CQLSH -u "$user" -p "$password" "$@"; }
works() { as "$1" "$2" -e "SELECT now() FROM system.local" >/dev/null 2>&1; }

# Cassandra takes about a minute to start, and creates its default login a few seconds after it
# answers. Wait until one of the logins works (up to 3 min).
LOGIN=""
for _ in $(seq 36); do
  if works "$ADMIN_USER" "$ADMIN_PASSWORD"; then LOGIN=admin; break; fi
  if [ "$PREVIOUS_ADMIN_PASSWORD" != "$ADMIN_PASSWORD" ] && works "$ADMIN_USER" "$PREVIOUS_ADMIN_PASSWORD"; then
    LOGIN=previous; break
  fi
  if works cassandra cassandra; then LOGIN=default; break; fi
  echo "waiting for Cassandra..."
  sleep 5
done
[ -n "$LOGIN" ] || { echo "Cassandra did not accept a login" >&2; exit 1; }

# Statements with a password go on stdin, not the command line, so they don't show in `ps`
if [ "$LOGIN" = previous ]; then
  echo "$ADMIN_USER still has its previous password: changing it"
  as "$ADMIN_USER" "$PREVIOUS_ADMIN_PASSWORD" <<< "ALTER ROLE $ADMIN_USER WITH PASSWORD = '$ADMIN_PASSWORD';"
fi

if [ "$LOGIN" = default ]; then
  echo "first run with logins on: creating $ADMIN_USER, then locking the default login"
  as cassandra cassandra <<< "CREATE ROLE IF NOT EXISTS $ADMIN_USER
    WITH PASSWORD = '$ADMIN_PASSWORD' AND SUPERUSER = true AND LOGIN = true;"
  RANDOM_PASSWORD=$(openssl rand -hex 16)   # never stored: nobody needs this login again
  as "$ADMIN_USER" "$ADMIN_PASSWORD" <<< \
    "ALTER ROLE cassandra WITH PASSWORD = '$RANDOM_PASSWORD' AND SUPERUSER = false AND LOGIN = false;"
fi

admin() { as "$ADMIN_USER" "$ADMIN_PASSWORD" "$@"; }
admin < deploy/cassandra/schema.cql   # on stdin, so no file has to exist inside the container

# set_login <role> <password>: make sure the login exists and takes this password. A new login is
# created with its password in one statement; an existing one is changed only if it doesn't take
# the password already, so a rotation reaches it. (Cassandra 4.1 refuses a second password change
# for the same role within 5 seconds, which "create, then always change" ran into.)
set_login() {
  local role=$1 password=$2
  if works "$role" "$password"; then
    echo "login $role: password already current"
  elif admin -e "SELECT role FROM system_auth.roles WHERE role = '$role'" | grep -qw "$role"; then
    admin <<< "ALTER ROLE $role WITH PASSWORD = '$password';"
    echo "login $role: password changed"
  else
    admin <<< "CREATE ROLE $role WITH PASSWORD = '$password' AND LOGIN = true;"
    echo "login $role: created"
  fi
}

# scoring may read and write its one table: no other table, no schema changes, no logins.
# (Cassandra's MODIFY also allows DELETE and TRUNCATE on that table; it has no finer permission.)
set_login scoring "$SCORING_PASSWORD"
admin -e "GRANT SELECT ON fraud.txn_by_user TO scoring"
admin -e "GRANT MODIFY ON fraud.txn_by_user TO scoring"
admin -e "LIST ALL PERMISSIONS OF scoring"
admin -e "DESCRIBE TABLE fraud.txn_by_user"

# features (the streaming feature job, docs/spark.md): writes its two tables, reads nothing.
# Its state lives in Spark's checkpoint, so it never needs to read Cassandra.
set_login features "$FEATURES_PASSWORD"
admin -e "GRANT MODIFY ON fraud.features_by_txn TO features"
admin -e "GRANT MODIFY ON fraud.user_features TO features"
admin -e "LIST ALL PERMISSIONS OF features"
