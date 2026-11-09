#!/usr/bin/env bash
# Start the cluster's payment data over with pseudonyms (week 5, step 2; threat 8, ST11).
# Design: docs/privacy.md. make k8s-reset-data
#
# ONE-WAY: earlier runs stored raw user ids. This removes every copy of them:
#   1. stops ingest, so no new payments arrive meanwhile
#   2. empties the Cassandra history and feature tables
#   3. deletes and recreates txn.raw, txn.scored and dlq.txn (their permissions stay: by name)
#   4. starts the feature job from a fresh checkpoint (its saved state held raw ids too)
#   5. rewrites cases.user_id to pseudonyms in place (cases, the audit log and the SIEM chain
#      stay; the audit rows never held user ids)
#   6. reloads the history (backfill), now pseudonymized, and starts ingest again
# Everything except the cases is rebuilt from data/processed/*.parquet. Asks for "yes" first.
set -euo pipefail
cd "$(dirname "$0")/.."
NS=fraud
JOB=statefulset/feature-job

read -r -p "This empties Cassandra's tables and Redpanda's payment topics on the cluster. Type yes: " answer
[ "$answer" = yes ] || { echo "nothing changed"; exit 1; }

# shellcheck source=scripts/k8s_secrets.sh
source scripts/k8s_secrets.sh
load_secrets
cql() { kubectl -n "$NS" exec -i cassandra-0 -c cassandra -- cqlsh -u fraud_admin -p "$CASSANDRA_ADMIN_PASSWORD" "$@"; }
psql_fraud() { kubectl -n "$NS" exec -i postgres-0 -c postgres -- psql -U fraud -d fraud -v ON_ERROR_STOP=1 -qtA "$@"; }

INGEST_COPIES=$(kubectl -n "$NS" get deploy ingest-svc -o jsonpath='{.spec.replicas}')
restore_ingest() { kubectl -n "$NS" scale deploy/ingest-svc --replicas="$INGEST_COPIES" >/dev/null; }
trap restore_ingest EXIT

echo "1/6 stop ingest (no new payments meanwhile)"
kubectl -n "$NS" scale deploy/ingest-svc --replicas=0
kubectl -n "$NS" wait --for=delete pod -l app=ingest-svc --timeout=120s || true

echo "2/6 empty the history and feature tables"
cql <<'CQL'
TRUNCATE fraud.txn_by_user;
TRUNCATE fraud.features_by_txn;
TRUNCATE fraud.user_features;
CQL

echo "3/6 recreate the payment topics, empty"
./scripts/rpk.sh topic delete txn.raw txn.scored dlq.txn </dev/null
RPK=./scripts/rpk.sh ./scripts/create_topics.sh </dev/null

echo "4/6 the feature job from a fresh checkpoint (its old state had raw ids)"
# Point it at a temporary folder, delete the old one, then back to its usual (now empty) folder
kubectl -n "$NS" set env $JOB CHECKPOINT=/checkpoint/reset >/dev/null
kubectl -n "$NS" rollout status $JOB --timeout=300s
kubectl -n "$NS" exec feature-job-0 -c feature-job -- rm -rf /checkpoint/feature-job
kubectl -n "$NS" set env $JOB CHECKPOINT=/checkpoint/feature-job >/dev/null
kubectl -n "$NS" rollout status $JOB --timeout=300s
kubectl -n "$NS" exec feature-job-0 -c feature-job -- rm -rf /checkpoint/reset

echo "5/6 cases: user ids to pseudonyms, with Postgres's own HMAC (pgcrypto, removed afterwards)"
# The same function as pseudonym.py; checked on one case first. The key goes on stdin.
psql_fraud <<<"CREATE EXTENSION IF NOT EXISTS pgcrypto;"
SQL_PSEUDONYM="CASE WHEN user_id ~ '^(NA\\|)?p1_[0-9a-f]{32}\$' THEN user_id
  WHEN 'NA' = ANY(string_to_array(user_id, '|'))
  THEN 'NA|p1_' || left(encode(hmac(user_id, :'key', 'sha256'), 'hex'), 32)
  ELSE 'p1_' || left(encode(hmac(user_id, :'key', 'sha256'), 'hex'), 32) END"
sample=$(psql_fraud <<SQL
\\set key '$PSEUDONYM_KEY'
SELECT user_id || ' ' || $SQL_PSEUDONYM FROM cases WHERE user_id !~ '^(NA\\|)?p1_' LIMIT 1;
SQL
)
if [ -n "$sample" ]; then
  raw=${sample% *} ours=${sample##* }
  want=$(PSEUDONYM_KEY="$PSEUDONYM_KEY" python pseudonym.py "$raw")
  [ "$ours" = "$want" ] || { echo "Postgres and pseudonym.py disagree; nothing changed" >&2; exit 1; }
fi
psql_fraud <<SQL
\\set key '$PSEUDONYM_KEY'
UPDATE cases SET user_id = $SQL_PSEUDONYM WHERE user_id !~ '^(NA\\|)?p1_[0-9a-f]{32}\$';
SQL
psql_fraud <<<"DROP EXTENSION pgcrypto;"
echo "cases left with a raw user id: $(psql_fraud <<<"SELECT count(*) FROM cases WHERE user_id !~ '^(NA\\|)?p1_[0-9a-f]{32}\$';")"

echo "6/6 reload the history (pseudonymized), then start ingest again"
./scripts/k8s_secrets.sh run ./scripts/k8s_tunnel.sh service/cassandra=9042:9042 -- python -m store.backfill
restore_ingest
kubectl -n "$NS" rollout status deploy/ingest-svc --timeout=300s
echo "done: run make k8s-st11"
