#!/usr/bin/env bash
# Scaling test (week 5, on the VM): the same work with 1, 2 and 4 scoring copies.
# Design and results: docs/results/scaling.md, "Week 5: 1, 2 and 4 copies".
# Output: data/scaling/<copies>/ and data/scaling/summary.md (not in git).
#
#   scripts/scaling_test.sh            1, 2 and 4 copies, 18,000 real payments each
#   scripts/scaling_test.sh plan       just print each run's rows
#   SENDERS=4 (default): sending processes per run, interleaved (load.replay --shard)
#   INGEST=4 (default): ingest copies held for the whole test, so ingest doesn't limit
#
# Each run holds scoring at exactly N copies (HPA min = max = N; put back at the end, whatever
# happens) and sends its payments as fast as CONCURRENCY in flight allow (a closed loop: the
# platform sets the pace). Throughput T_N is what got answered per second in the steady middle of
# the run (load/scaling.py); E_N = T_N / (N x T_1).
set -euo pipefail
cd "$(dirname "$0")/.."

NS=fraud
URL=http://localhost:8080
PY=${PYTHON:-python}
COPIES=${COPIES:-1 2 4}
COUNT=${COUNT:-18000}             # payments per run: fixed work, so a faster platform finishes sooner
CONCURRENCY=${CONCURRENCY:-64}    # in flight per sender: enough that the platform sets the pace
# Sending processes, each with every SENDERS-th row (load.replay --shard): one Python sender tops
# out near 150/s (the first VM ramp), below what 2 or 4 scoring copies may answer
SENDERS=${SENDERS:-4}
# ingest held at this many copies for the whole test (put back afterwards). At its usual 2 copies
# ingest was saturated near 250/s on the 8-core VM (11 Nov), so scoring wasn't what limited: with
# 4, scoring is the part that varies and the part that limits
INGEST=${INGEST:-4}
# This test's rows of stream.parquet (docs/results/ramp.md has the whole plan)
FIRST_ROW=110000
LAST_ROW=167240
OUT=data/scaling

offsets() {   # one line per run: <copies> <first row>
  local row=$FIRST_ROW n
  for n in $COPIES; do
    echo "$n $row"
    row=$(( row + COUNT ))
  done
  if [ "$row" -gt "$LAST_ROW" ]; then
    echo "COPIES=\"$COPIES\" x $COUNT payments need rows $FIRST_ROW-$row; this test has $FIRST_ROW-$LAST_ROW" >&2
    return 1
  fi
}

[[ "$SENDERS" =~ ^[1-9][0-9]*$ ]] || { echo "SENDERS must be 1 or more" >&2; exit 1; }
[[ "$INGEST" =~ ^[1-9][0-9]*$ ]] || { echo "INGEST must be 1 or more" >&2; exit 1; }
PLAN=$(offsets)   # an assignment, so a refusal stops here
if [ "${1:-}" = plan ]; then
  echo "$PLAN" | while read -r n row; do echo "$n copies: rows $row +$COUNT"; done
  exit 0
fi

if [ "${SCALING_INNER:-}" != 1 ]; then
  export SCALING_INNER=1
  exec scripts/k8s_tunnel.sh service/prometheus=9090:9090 -- "$0" "$@"
fi

ORIG_HPA=$(kubectl -n "$NS" get hpa scoring-svc -o jsonpath='{.spec.minReplicas},{.spec.maxReplicas}')
ORIG_INGEST=$(kubectl -n "$NS" get deploy ingest-svc -o jsonpath='{.spec.replicas}')
CPU_LOG=""
restore() {
  [ -n "$CPU_LOG" ] && kill "$CPU_LOG" 2>/dev/null
  IFS=, read -r min max <<< "$ORIG_HPA"
  kubectl -n "$NS" patch hpa scoring-svc -p "{\"spec\":{\"minReplicas\":${min:-1},\"maxReplicas\":$max}}" >/dev/null
  echo "HPA back to ${min:-1}-$max copies"
  kubectl -n "$NS" scale deploy/ingest-svc --replicas="$ORIG_INGEST" >/dev/null
  echo "ingest back to $ORIG_INGEST copies"
}
trap restore EXIT

ready() { kubectl -n "$NS" get deploy scoring-svc -o jsonpath='{.status.readyReplicas}'; }
hold_at() {   # hold_at <n>: exactly n scoring copies, all ready
  local n=$1 i
  kubectl -n "$NS" patch hpa scoring-svc -p "{\"spec\":{\"minReplicas\":$n,\"maxReplicas\":$n}}" >/dev/null
  for i in $(seq 120); do
    if [ "$(kubectl -n "$NS" get deploy scoring-svc -o jsonpath='{.spec.replicas}')" = "$n" ] \
       && [ "$(ready)" = "$n" ] \
       && [ "$(kubectl -n "$NS" get pods -l app=scoring-svc --no-headers | wc -l | tr -d ' ')" = "$n" ]; then
      echo "scoring at $n copies, all ready (after $(( i * 5 )) s)"
      return 0
    fi
    sleep 5
  done
  echo "scoring didn't settle at $n copies in 10 minutes" >&2; return 1
}

mkdir -p "$OUT"
rm -rf "${OUT:?}"/*
echo "ingest held at $INGEST copies"
kubectl -n "$NS" scale deploy/ingest-svc --replicas="$INGEST" >/dev/null
kubectl -n "$NS" rollout status deploy/ingest-svc --timeout=300s
# The whole VM's CPU every 2 s (/proc/stat: the cluster and the senders together), so a run that
# fills the machine shows it
"$PY" -m load.scaling cpu-log "$OUT/vm_cpu.csv" < /dev/null &
CPU_LOG=$!
while read -r n row; do
  dir=$OUT/$n
  mkdir -p "$dir"
  echo "== $n scoring copies: $COUNT payments from row $row, $SENDERS senders x $CONCURRENCY in flight"
  hold_at "$n"
  sleep 20   # new pods: let their first calls (connections, the model library) pass before measuring
  pids=()
  for i in $(seq 0 $(( SENDERS - 1 ))); do
    "$PY" -m load.replay --speed 0 --offset "$row" --limit "$COUNT" --shard "$i/$SENDERS" \
      --concurrency "$CONCURRENCY" --url "$URL" --out "$dir/run-$i.csv" \
      < /dev/null > "$dir/client-$i.txt" 2>&1 &
    pids+=($!)
  done
  for pid in "${pids[@]}"; do wait "$pid" || { echo "a sender failed: $dir/client-*.txt" >&2; exit 1; }; done
  [ "$(ready)" = "$n" ] || echo "warning: $(ready) copies ready at the end, not $n" | tee -a "$dir/client-0.txt"
  # The run's window on one clock: from the first sender's start to the last one's finish
  window=$("$PY" - "$dir" <<'EOF2'
import re, sys
from pathlib import Path
spans = []
for f in Path(sys.argv[1]).glob("client-*.txt"):
    t = f.read_text()
    start = float(re.search(r"Started sending at unix ([\d.]+)", t).group(1))
    spans.append((start, start + float(re.search(r"payments in ([\d.]+) s", t).group(1))))
first, last = min(s for s, _ in spans), max(e for _, e in spans)
print(max(30, int(last - first)), int(round(last)))
EOF2
)
  read -r seconds end <<< "$window"
  echo "{\"start\": $(( end - seconds )), \"end\": $end}" > "$dir/window.json"
  sleep 10   # the last Prometheus samples of the run
  "$PY" -m observability.snapshot --seconds "$seconds" --end "$end" \
    --json "$dir/prometheus.json" < /dev/null > "$dir/prometheus.md"
  grep -h "^Sent\|^Latency" "$dir"/client-*.txt
done <<< "$PLAN"

"$PY" -m load.scaling report "$OUT" | tee "$OUT/summary.md"
echo "Saved: $OUT/summary.md"
