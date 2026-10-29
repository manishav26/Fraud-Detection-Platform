#!/usr/bin/env bash
# Creates the Redpanda topics (week 2, step 1). Safe to run again: topics that exist are skipped.
# Topic list and reasons: docs/topics.md
set -euo pipefail
cd "$(dirname "$0")/.."

# 1 copy of each message while we run a single broker; set TOPIC_REPLICAS=3 on a 3-broker cluster.
REPLICAS="${TOPIC_REPLICAS:-1}"

# name  partitions  [extra config]
TOPICS=(
  "txn.raw       6"
  "txn.scored    6"
  "labels        3"
  "model.events  1  cleanup.policy=compact"
  "dlq.txn       1"
)

# How to run rpk: in the compose container by default; scripts/k8s_up.sh points it at the pod.
RPK="${RPK:-docker compose exec -T redpanda rpk}"
rpk() { $RPK "$@"; }

existing=$(rpk topic list | awk 'NR > 1 {print $1}')

for spec in "${TOPICS[@]}"; do
  read -r name partitions config <<<"$spec"
  if grep -qx "$name" <<<"$existing"; then
    echo "exists:  $name"
    continue
  fi
  args=(topic create "$name" --partitions "$partitions" --replicas "$REPLICAS")
  if [ -n "$config" ]; then
    args+=(--topic-config "$config")
  fi
  rpk "${args[@]}"
done

rpk topic list
