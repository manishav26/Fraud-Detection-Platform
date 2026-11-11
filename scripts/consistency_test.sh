#!/usr/bin/env bash
# Consistency test, ONE vs LOCAL_QUORUM (week 5, on the VM's 3-node Cassandra). Design and results:
# docs/results/consistency.md. Output: data/consistency/<condition>-<level>-<round>/ (not in git).
#
#   scripts/consistency_test.sh ONE|LOCAL_QUORUM healthy 1|2    3 min of payments through Kong,
#                                                              and 5,000 write-then-read checks
#   scripts/consistency_test.sh ONE|LOCAL_QUORUM behind 1       cassandra-2 out for 3 min (3,000
#                                                              test ids written), then back for
#                                                              3 min without catching up (read back)
#   scripts/consistency_test.sh plan <level> <condition> <round>    just print the rows it would send
#
# Scoring runs at the level under test (CASSANDRA_CONSISTENCY, reads and writes), at a fixed 2
# copies (HPA min = max = 2) so a scale-up can't change one run's p95. Whatever happens, the end
# puts back the HPA, scoring's level and, for "behind", the node, hinted handoff and a full repair.
# RPS (default 30) and MINUTES (per phase, default 3) can be changed, within the rows below.
set -euo pipefail
cd "$(dirname "$0")/.."

NS=fraud
URL=http://localhost:8080
PY=${PYTHON:-python}
RPS=${RPS:-30}
MINUTES=${MINUTES:-3}
PHASE=$(( RPS * 60 * MINUTES ))     # payments per phase
# This test's own rows of stream.parquet (210,440): the last 43,200. Ramp and scaling use rows
# before 167,240, so no payment is sent twice on the VM.
FIRST_ROW=167240
LAST_ROW=210440
PAIRS=5000      # healthy: write-then-read checks per run
SEEDED=3000     # behind: test ids written while the node is out

usage() { sed -n 4,12p "$0" >&2; exit 1; }

# Each run's slot in the rows: healthy 1 ONE, 1 QUORUM, 2 ONE, 2 QUORUM, then behind ONE (2 phases),
# behind QUORUM (2 phases)
slot() {   # slot <level> <condition> <round> -> first phase number
  local level=$1 condition=$2 round=$3 l
  [ "$level" = ONE ] && l=0 || l=1
  case "$condition:$round" in
    healthy:1|healthy:2) echo $(( (round - 1) * 2 + l )) ;;
    behind:1) echo $(( 4 + l * 2 )) ;;
    *) echo "condition must be healthy (round 1 or 2) or behind (round 1)" >&2; return 1 ;;
  esac
}

offset_of() {   # offset_of <level> <condition> <round> <phase within the run: 0 or 1>
  local s
  s=$(slot "$1" "$2" "$3") || return 1
  # All 6 runs (8 phases) must fit at the same RPS and MINUTES: the runs are only comparable at
  # one rate, and a run at another rate would send rows another run uses
  if [ $(( FIRST_ROW + 8 * PHASE )) -gt "$LAST_ROW" ]; then
    echo "RPS=$RPS and MINUTES=$MINUTES: the 8 phases need $(( 8 * PHASE )) rows, this test has $(( LAST_ROW - FIRST_ROW )) ($FIRST_ROW-$LAST_ROW)" >&2
    return 1
  fi
  echo $(( FIRST_ROW + (s + $4) * PHASE ))
}

check_args() {
  case "${1:-}" in ONE|LOCAL_QUORUM) ;; *) usage ;; esac
  case "${2:-}" in healthy|behind) ;; *) usage ;; esac
  slot "$1" "$2" "${3:-}" >/dev/null || exit 1
}

if [ "${1:-}" = plan ]; then
  shift
  check_args "$@"
  phases=1; [ "$2" = behind ] && phases=2
  for p in $(seq 0 $(( phases - 1 ))); do
    offset=$(offset_of "$1" "$2" "$3" "$p")   # an assignment, so a refusal stops here
    echo "phase $p: rows $offset +$PHASE"
  done
  exit 0
fi

LEVEL=${1:-}; CONDITION=${2:-}; ROUND=${3:-}
check_args "$LEVEL" "$CONDITION" "$ROUND"
# Both phases' rows, checked before anything in the cluster changes
ROWS0=$(offset_of "$LEVEL" "$CONDITION" "$ROUND" 0)
ROWS1=$ROWS0
[ "$CONDITION" = behind ] && ROWS1=$(offset_of "$LEVEL" "$CONDITION" "$ROUND" 1)
nt() { kubectl -n "$NS" exec "$1" -c cassandra -- nodetool "${@:2}"; }
pod_ip() { kubectl -n "$NS" get pod "$1" -o jsonpath='{.status.podIP}'; }

# 1. Outside the tunnels: check the cluster, then start again inside them (one tunnel per Cassandra
#    pod on its own loopback address, and Prometheus), with the Cassandra admin password.
if [ "${CONSISTENCY_INNER:-}" != 1 ]; then
  [ "$(kubectl -n "$NS" get sts cassandra -o jsonpath='{.status.readyReplicas}')" = 3 ] || {
    echo "needs the 3-node Cassandra, all ready: OVERLAY=vm make k8s-up (docs/vm.md)" >&2; exit 1; }
  up=$(nt cassandra-0 status | grep -c '^UN' || true)
  [ "$up" = 3 ] || { echo "nodetool status shows $up of 3 nodes up (UN)" >&2; exit 1; }
  tunnels=() map=()
  for n in 0 1 2; do
    tunnels+=("pod/cassandra-$n=127.0.0.$(( n + 2 )):9042:9042")
    map+=("cassandra-$n=$(pod_ip "cassandra-$n")=127.0.0.$(( n + 2 ))")
  done
  CASSANDRA_TUNNELS=$(IFS=,; echo "${map[*]}")
  export CASSANDRA_TUNNELS CONSISTENCY_INNER=1
  exec scripts/k8s_secrets.sh run scripts/k8s_tunnel.sh "${tunnels[@]}" \
    service/prometheus=9090:9090 -- "$0" "$LEVEL" "$CONDITION" "$ROUND"
fi

# 2. Inside the tunnels
OUT=data/consistency/$CONDITION-$LEVEL-$ROUND
RUN_ID=$CONDITION-$LEVEL-$ROUND-$(date +%s)   # new test ids on every run
mkdir -p "$OUT"
rm -f "$OUT"/*   # this run's files only: an earlier attempt's must not end up in its summary
EVENTS=$OUT/events.txt
: > "$EVENTS"
note() { echo "$(date '+%H:%M:%S') $*" | tee -a "$EVENTS"; }

ORIG_HPA=$(kubectl -n "$NS" get hpa scoring-svc -o jsonpath='{.spec.minReplicas},{.spec.maxReplicas}')
NODE_OUT=0 HANDOFF_OFF=0 REPAIR_DUE=0 EXTRA_TUNNEL=""

node_state() {   # node_state <asking pod> <ip>: UN, DN, ... as that node sees it
  nt "$1" status | awk -v ip="$2" '$2 == ip {print $1}'
}

wait_state() {   # wait_state <ip> <state> <seconds>: until cassandra-0 and -1 both see it
  local ip=$1 want=$2 i
  for i in $(seq "$3"); do
    if [ "$(node_state cassandra-0 "$ip")" = "$want" ] && [ "$(node_state cassandra-1 "$ip")" = "$want" ]; then
      note "cassandra-0 and -1 see $ip as $want (after ${i} s)"
      return 0
    fi
    sleep 1
  done
  note "cassandra-0 and -1 did not see $ip as $want within $3 s"; return 1
}

restore() {   # runs on every exit, also after a failure; each step is safe to repeat
  set +e
  if [ "$NODE_OUT" = 1 ]; then
    note "putting cassandra-2 back"
    nt cassandra-2 enablegossip; nt cassandra-2 enablebinary
  fi
  if [ "$HANDOFF_OFF" = 1 ]; then
    note "hinted handoff back on"
    nt cassandra-0 enablehandoff; nt cassandra-1 enablehandoff
  fi
  if [ "$REPAIR_DUE" = 1 ]; then
    note "full repair of fraud (cassandra-2 missed writes)"
    nt cassandra-2 repair -full fraud >> "$EVENTS" 2>&1 && note "repair done"
  fi
  note "scoring back to its own level (the manifest's LOCAL_QUORUM)"
  kubectl -n "$NS" set env deployment/scoring-svc CASSANDRA_CONSISTENCY-
  kubectl -n "$NS" rollout status deployment/scoring-svc --timeout=300s
  IFS=, read -r min max <<< "$ORIG_HPA"
  min=${min:-1}   # unset in the manifest means Kubernetes' default, 1
  kubectl -n "$NS" patch hpa scoring-svc -p "{\"spec\":{\"minReplicas\":$min,\"maxReplicas\":$max}}"
  note "HPA back to $min-$max copies"
  [ -n "$EXTRA_TUNNEL" ] && kill "$EXTRA_TUNNEL" 2>/dev/null
  return 0
}
trap restore EXIT

note "start: $CONDITION, $LEVEL, round $ROUND, $RPS/s, $MINUTES min per phase, run id $RUN_ID"
kubectl -n "$NS" patch hpa scoring-svc -p '{"spec":{"minReplicas":2,"maxReplicas":2}}'
kubectl -n "$NS" set env deployment/scoring-svc "CASSANDRA_CONSISTENCY=$LEVEL"
kubectl -n "$NS" rollout status deployment/scoring-svc --timeout=300s
for pod in $(kubectl -n "$NS" get pods -l app=scoring-svc --field-selector=status.phase=Running -o name); do
  note "$pod: CASSANDRA_CONSISTENCY=$(kubectl -n "$NS" exec "$pod" -c scoring-svc -- printenv CASSANDRA_CONSISTENCY)"
done

# The checker's reads must have reached the nodes this run is about, or a "0 stale" would mean
# only that the reads went elsewhere (a closed tunnel, say)
expect_reads() {   # expect_reads <check.json> <python condition on `nodes`> <what it means>
  "$PY" - "$1" "$2" "$3" <<'EOF2'
import json, sys
nodes = json.load(open(sys.argv[1]))["reads_by_node"]
if not eval(sys.argv[2], {}, {"nodes": nodes}):
    sys.exit(f"checker reads by node {nodes}: expected {sys.argv[3]}")
print(f"checker reads by node {nodes}: {sys.argv[3]}, as expected")
EOF2
}

# kubectl port-forward can end when the pod refuses a connection (cassandra-2 while it is out);
# the reads after it is back must reach it, so open that tunnel again if it closed
ensure_tunnel_c2() {
  nc -z 127.0.0.4 9042 2>/dev/null && return 0
  note "the tunnel to cassandra-2 had closed: opening it again"
  kubectl -n "$NS" port-forward --address 127.0.0.4 pod/cassandra-2 9042:9042 >/dev/null &
  EXTRA_TUNNEL=$!
  for _ in $(seq 40); do nc -z 127.0.0.4 9042 2>/dev/null && return 0; sleep 0.25; done
  note "could not open the tunnel to cassandra-2"; return 1
}

phase() {   # phase <name> <row offset> <checker mode> <checker count>
  local name=$1 offset=$2 mode=$3 count=$4 start end replay=0 checker=0
  local seconds=$(( MINUTES * 60 ))
  local rate
  rate=$("$PY" -c "print(round($count / $seconds, 2))")
  note "$name: $PHASE payments from row $offset at $RPS/s; checker $mode, $count test ids at $rate/s"
  start=$(date +%s)
  "$PY" -m load.replay --rps "$RPS" --offset "$offset" --limit "$PHASE" --url "$URL" \
    --out "$OUT/$name-replay.csv" > "$OUT/$name-client.txt" 2>&1 &
  local r=$!
  "$PY" -m load.consistency_check "$mode" --level "$LEVEL" --run "$RUN_ID" --count "$count" \
    --rate "$rate" --out "$OUT/$name-check.json" > "$OUT/$name-check.txt" 2>&1 &
  local c=$!
  wait "$r" || replay=$?
  wait "$c" || checker=$?
  end=$(date +%s)
  note "$name finished in $(( end - start )) s (replay exit $replay, checker exit $checker)"
  "$PY" -m observability.snapshot --seconds $(( end - start )) --end "$end" > "$OUT/$name-prometheus.md"
  [ "$replay" = 0 ] && [ "$checker" = 0 ]
}

if [ "$CONDITION" = healthy ]; then
  phase healthy "$ROWS0" pairs "$PAIRS"
  seen=$(expect_reads "$OUT/healthy-check.json" "len(nodes) == 3" "all 3 nodes answered reads")
  note "$seen"
else
  C2=$(pod_ip cassandra-2)
  note "hinted handoff off on cassandra-0 and -1, so cassandra-2 stays behind once back"
  HANDOFF_OFF=1
  nt cassandra-0 disablehandoff; nt cassandra-1 disablehandoff
  for n in 0 1; do note "cassandra-$n: $(nt "cassandra-$n" statushandoff)"; done
  note "cassandra-2 ($C2) out: no clients, no gossip"
  NODE_OUT=1 REPAIR_DUE=1
  nt cassandra-2 disablebinary; nt cassandra-2 disablegossip
  wait_state "$C2" DN 120
  phase out "$ROWS0" seed "$SEEDED"
  note "cassandra-2 back, without the writes it missed"
  nt cassandra-2 enablegossip; nt cassandra-2 enablebinary
  NODE_OUT=0
  wait_state "$C2" UN 180
  note "cassandra-2 statusbinary: $(nt cassandra-2 statusbinary)"
  ensure_tunnel_c2
  phase back "$ROWS1" read "$SEEDED"
  seen=$(expect_reads "$OUT/back-check.json" "nodes.get('cassandra-2', 0) > 0" "cassandra-2 answered some reads")
  note "$seen"
  nt cassandra-0 enablehandoff; nt cassandra-1 enablehandoff
  HANDOFF_OFF=0
  note "hinted handoff back on; full repair of fraud"
  nt cassandra-2 repair -full fraud >> "$EVENTS" 2>&1
  REPAIR_DUE=0
  note "repair done; reading the same test ids again at ONE (expect none stale)"
  "$PY" -m load.consistency_check read --level ONE --run "$RUN_ID" --count "$SEEDED" \
    --out "$OUT/after-repair-check.json" > "$OUT/after-repair-check.txt" 2>&1
  seen=$(expect_reads "$OUT/after-repair-check.json" "nodes.get('cassandra-2', 0) > 0" "cassandra-2 answered some reads")
  note "$seen"
fi

# Put everything back before writing the summary, so its notes are in it too
trap - EXIT
restore

{
  echo "# Consistency test: $CONDITION, $LEVEL, round $ROUND ($(date '+%Y-%m-%d'))"
  echo
  echo "$RPS/s through Kong, $MINUTES min per phase, run id $RUN_ID"
  for f in "$OUT"/*-check.json; do
    echo; echo "## Checker: $(basename "$f" -check.json)"; echo '```'; cat "$f"; echo '```'
  done
  for f in "$OUT"/*-client.txt; do
    echo; echo "## Client: $(basename "$f" -client.txt)"; echo '```'; cat "$f"; echo '```'
  done
  for f in "$OUT"/*-prometheus.md; do
    echo; echo "## Prometheus: $(basename "$f" -prometheus.md)"; echo; cat "$f"
  done
  echo; echo "## What happened"; echo '```'; cat "$EVENTS"; echo '```'
} > "$OUT/summary.md"
note "saved $OUT/summary.md"
