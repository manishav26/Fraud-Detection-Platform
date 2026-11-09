#!/usr/bin/env bash
# ST4, database logins: what each Cassandra login may and may not do (threat 4; docs/cassandra.md).
#   make k8s-st4-db        (cluster)        make st4-db        (compose)
# Runs cqlsh inside the Cassandra pod or container. Changes nothing: every allowed statement
# is a read, and every refused one is refused before it runs. Exit code 1 if anything differs.
set -uo pipefail
cd "$(dirname "$0")/../.."

CQLSH="${CQLSH:-docker compose exec -T cassandra cqlsh}"
FAILED=0
printf '| Login | Statement | Expected | Got |\n|---|---|---|---|\n'

check() {   # check <user> <password> <expect: ok|refused> <statement>
  local user=$1 password=$2 expect=$3 cql=$4 out got
  out=$($CQLSH -u "$user" -p "$password" -e "$cql" 2>&1)
  if [ $? -eq 0 ] && ! grep -qiE "unauthorized|error|not authorized|bad credentials" <<< "$out"; then
    got=ok
  else
    got=refused
  fi
  local mark=""
  [ "$got" = "$expect" ] || { mark=" WRONG"; FAILED=1; }
  printf '| %s | `%s` | %s | %s%s |\n' "${user:-none}" "$cql" "$expect" "$got" "$mark"
}

ROW_CHECK="SELECT txn_id FROM fraud.txn_by_user LIMIT 1"
check scoring scoring_dev_only ok "$ROW_CHECK"
check scoring scoring_dev_only refused "SELECT role FROM system_auth.roles"
check scoring scoring_dev_only refused "CREATE TABLE fraud.st4_probe (id int PRIMARY KEY)"
check scoring scoring_dev_only refused "DROP TABLE fraud.txn_by_user"
check scoring scoring_dev_only refused "CREATE KEYSPACE st4_probe WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1}"
check scoring scoring_dev_only refused "CREATE ROLE st4_probe WITH PASSWORD = 'x' AND LOGIN = true"
check scoring scoring_dev_only refused "GRANT SELECT ON ALL KEYSPACES TO scoring"
check scoring wrong_password refused "$ROW_CHECK"
check cassandra cassandra refused "$ROW_CHECK"
check fraud_admin fraud_admin_dev_only ok "$ROW_CHECK"

if [ "$FAILED" -ne 0 ]; then
  echo; echo "ST4 (database logins) FAILED"; exit 1
fi
echo; echo "ST4 (database logins) PASSED: scoring can only read and write its one table"
