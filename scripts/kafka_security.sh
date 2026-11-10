#!/usr/bin/env bash
# Redpanda users and topic permissions (week 5, step 1; threat 13). Design: docs/kafka.md
# Run by scripts/k8s_up.sh (step 4) with the passwords loaded (scripts/k8s_secrets.sh). Safe to run
# again: users are created or get their current password, permissions already there stay.
#
# Order matters: Redpanda refuses a login while logins are off, and everyone without one once
# they're on. So: the superuser and the users first, then the permissions, then logins on.
set -euo pipefail
cd "$(dirname "$0")/.."
NS=${NS:-fraud}
in_pod() { kubectl -n "$NS" exec -i redpanda-0 -c redpanda -- "$@"; }
rpk() { ./scripts/rpk.sh "$@"; }   # as admin once logins are on

# user:variable holding its password
USERS=(
  admin:REDPANDA_ADMIN_PASSWORD
  ingest:REDPANDA_INGEST_PASSWORD
  scoring:REDPANDA_SCORING_PASSWORD
  alert-svc:REDPANDA_ALERTS_PASSWORD
  feature-job:REDPANDA_FEATURES_PASSWORD
  model-admin:REDPANDA_MODEL_ADMIN_PASSWORD
  tools:REDPANDA_TOOLS_PASSWORD
  label-sim:REDPANDA_LABEL_SIM_PASSWORD
)

# 1. The superuser: everything, for rpk only. Set before logins go on, or no one could manage them.
in_pod rpk cluster config set superusers '["admin"]' >/dev/null

# 2. Users, through the admin API (open inside the pod and to Prometheus only: docs/kafka.md).
# Each password goes in on stdin; it is an argument only to rpk inside the Redpanda container.
existing=$(in_pod rpk security user list | awk 'NR > 1 {print $1}')
for entry in "${USERS[@]}"; do
  user=${entry%%:*} var=${entry#*:}
  password=${!var:?$var is not set: run through scripts/k8s_up.sh}
  if grep -qx -- "$user" <<<"$existing"; then
    set -- update --new-password
  else
    set -- create --password
  fi
  printf '%s\n' "$password" | in_pod sh -c \
    'IFS= read -r p; exec rpk security user "$1" "$2" "$3" "$p" --mechanism SCRAM-SHA-256 >/dev/null' \
    _ "$1" "$user" "$2"
  echo "redpanda user $user: $1d"
done

# 3. Permissions: each workload gets only what it needs (the table in docs/kafka.md). Anything not
# allowed here is refused. Idempotent producers also need idempotent_write on the cluster.
acl() { rpk security acl create "$@" >/dev/null; }
acl --allow-principal ingest --operation write,describe --topic txn.raw,txn.scored,dlq.txn
acl --allow-principal ingest --operation idempotent_write --cluster
acl --allow-principal scoring --operation read,describe --topic model.events
acl --allow-principal alert-svc --operation read,describe --topic txn.scored
acl --allow-principal alert-svc --operation read,describe --group alert-svc
acl --allow-principal feature-job --operation read,describe --topic txn.raw,txn.parity,txn.bench
acl --allow-principal feature-job --operation read,describe --group spark-kafka-source \
  --resource-pattern-type prefixed
acl --allow-principal model-admin --operation write,describe --topic model.events
acl --allow-principal model-admin --operation idempotent_write --cluster
acl --allow-principal tools --operation write,describe --topic txn.parity,txn.bench
# The label service identity (threat 10, ST13): the only one that may write fraud answers
acl --allow-principal label-sim --operation write,describe --topic labels
echo "redpanda permissions: set for ${#USERS[@]} users"

# 4. Logins on (no restart needed); this also turns permission checks on
in_pod rpk cluster config set enable_sasl true >/dev/null
echo "redpanda logins: on"
