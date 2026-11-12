#!/usr/bin/env bash
# Failure tests (week 5, on the VM): kill one part under steady load, see what clients notice and
# how long until it's back. Design and results: docs/results/failures.md.
# Output: data/failures/<kind>-<round>/ and data/failures/summary.md (not in git).
#
#   scripts/failure_test.sh scoring 1      delete one scoring pod (the other copy keeps answering)
#   scripts/failure_test.sh cassandra 1    delete cassandra-1 (2 of 3 copies left: LOCAL_QUORUM holds)
#   scripts/failure_test.sh redpanda 1     delete redpanda-0 (the only broker: payments can't be saved)
#   scripts/failure_test.sh report         the table and availability over every run so far
#
# Load: synthetic payments (load.synthetic: no rows of the stream are used up) at RPS (default 30)
# for MINUTES (default 4), sent as the load-test client; the kill comes at KILL_AT seconds (default
# 60). "Back" is Kubernetes' view: the pod ready again (Cassandra: the node Up/Normal; Redpanda:
# the topics answer). What clients felt is in the payments (load/failures.py). ROUND numbers the
# repeats (1, 2, 3, ...).
set -euo pipefail
cd "$(dirname "$0")/.."

NS=fraud
URL=http://localhost:8080
PY=${PYTHON:-python}
RPS=${RPS:-30}
MINUTES=${MINUTES:-4}
KILL_AT=${KILL_AT:-60}
OUT=data/failures

if [ "${1:-}" = report ]; then
  "$PY" -m load.failures report "$OUT" | tee "$OUT/summary.md"
  exit 0
fi
KIND=${1:-}; ROUND=${2:-}
case "$KIND" in scoring|cassandra|redpanda) ;; *) sed -n 5,9p "$0" >&2; exit 1 ;; esac
[[ "$ROUND" =~ ^[0-9]+$ ]] || { echo "ROUND must be a number: 1, 2, 3, ..." >&2; exit 1; }
[ "$KILL_AT" -lt $(( MINUTES * 60 - 60 )) ] || { echo "KILL_AT must leave at least a minute after it" >&2; exit 1; }

DIR=$OUT/$KIND-$ROUND
rm -rf "$DIR"; mkdir -p "$DIR"
note() { echo "$(date '+%H:%M:%S') $*" | tee -a "$DIR/events.txt"; }
ready_count() { kubectl -n "$NS" get deploy "$1" -o jsonpath='{.status.readyReplicas}'; }
pod_ready() { [ "$(kubectl -n "$NS" get pod "$1" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" = True ]; }

# Everything healthy before starting, or the run would measure an earlier problem
[ "$(kubectl -n "$NS" exec cassandra-0 -c cassandra -- nodetool status | grep -c '^UN')" = 3 ] || {
  echo "Cassandra isn't 3 nodes Up/Normal: not starting" >&2; exit 1; }
pod_ready redpanda-0 || { echo "redpanda-0 isn't ready: not starting" >&2; exit 1; }
SCORING_COPIES=$(ready_count scoring-svc)
[ "${SCORING_COPIES:-0}" -ge 2 ] || { echo "fewer than 2 scoring copies ready: not starting" >&2; exit 1; }

note "start: $KIND, round $ROUND, $RPS/s for $MINUTES min, kill at $KILL_AT s"
"$PY" -m load.synthetic --rps "$RPS" --seconds "$(( MINUTES * 60 ))" --seed "$(( 500 + ROUND ))" \
  --url "$URL" --out "$DIR/run.csv" < /dev/null > "$DIR/client.txt" 2>&1 &
LOAD=$!
trap 'kill $LOAD 2>/dev/null || true' EXIT
sleep "$KILL_AT"

case "$KIND" in
  scoring)
    TARGET=$(kubectl -n "$NS" get pods -l app=scoring-svc -o jsonpath='{.items[0].metadata.name}')
    is_back() { ! kubectl -n "$NS" get pod "$TARGET" >/dev/null 2>&1 \
                && [ "$(ready_count scoring-svc)" = "$SCORING_COPIES" ]; } ;;
  cassandra)
    TARGET=cassandra-1
    is_back() { pod_ready cassandra-1 && [ "$(kubectl -n "$NS" exec cassandra-0 -c cassandra -- \
                nodetool status 2>/dev/null | grep -c '^UN')" = 3 ]; } ;;
  redpanda)
    TARGET=redpanda-0
    # </dev/null: rpk.sh passes its stdin on to rpk, and here there is nothing to pass
    is_back() { pod_ready redpanda-0 && ./scripts/rpk.sh topic describe txn.raw </dev/null >/dev/null 2>&1; } ;;
esac
KILLED=$(date +%s.%N)
note "delete pod $TARGET"
kubectl -n "$NS" delete pod "$TARGET" --wait=false
sleep 2   # the pod must be seen going before "back" can mean anything
for _ in $(seq 600); do
  is_back && break
  sleep 1
done
BACK=$(date +%s.%N)
is_back || note "WARNING: not back within 10 minutes"
note "$TARGET back after $("$PY" -c "print(round($BACK - $KILLED, 1))") s"
echo "{\"kind\": \"$KIND\", \"target\": \"$TARGET\", \"killed\": $KILLED, \"back\": $BACK}" > "$DIR/events.json"

wait "$LOAD"
trap - EXIT
note "load finished"
tail -9 "$DIR/client.txt"
"$PY" -m load.failures report "$OUT" | tee "$OUT/summary.md"
