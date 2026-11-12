#!/usr/bin/env bash
# Ramp test (week 5, on the VM): raise the payment rate in steps until a target is missed.
# Design and results: docs/results/ramp.md. Output: data/ramp/<rate>/ and data/ramp/summary.md
# (not in git).
#
#   scripts/ramp_test.sh                 steps of 25, 50, 75, 100, 150, 200 payments a second
#   scripts/ramp_test.sh plan            just print each step's rows
#   SENDERS=2 scripts/ramp_test.sh       two sending processes (make k8s-ramp SENDERS=2)
#
# Each step sends real payments through Kong for MINUTES (default 3) and is measured over its last
# MEASURE seconds (default 120), so the first minute absorbs the change of rate and any scale-up.
# The ramp stops after the first step that misses a target (load/ramp.py), or whose sender didn't
# reach the rate. The HPA stays as deployed: the platform as it runs. RATES can be changed, within
# this test's rows.
set -euo pipefail
cd "$(dirname "$0")/.."

URL=http://localhost:8080
PY=${PYTHON:-python}
RATES=${RATES:-25 50 75 100 150 200}
MINUTES=${MINUTES:-3}
MEASURE=${MEASURE:-120}
CONCURRENCY=${CONCURRENCY:-256}   # payments in flight, per sender: slow answers must not hold it back
# Sending processes per step, each with every SENDERS-th row at rate / SENDERS (load.replay
# --shard): together the stream in time order at the full rate. One Python sender tops out near
# 150/s (the first VM ramp, 11 Nov: asked 200, reached 148.6 while Kong's p95 stayed at 91 ms)
SENDERS=${SENDERS:-1}
SECONDS_PER_STEP=$(( MINUTES * 60 ))
# This test's rows of stream.parquet: the VM's check replay used 0-1,999; scaling uses
# 110,000-167,239 and the consistency test 167,240 onward
FIRST_ROW=2000
LAST_ROW=110000
OUT=data/ramp

offsets() {   # one line per step: <rate> <first row>; refuses steps that need rows past LAST_ROW
  local row=$FIRST_ROW rate
  for rate in $RATES; do
    echo "$rate $row"
    row=$(( row + rate * SECONDS_PER_STEP ))
  done
  if [ "$row" -gt "$LAST_ROW" ]; then
    echo "RATES=\"$RATES\" for $MINUTES min each need rows $FIRST_ROW-$row; this test has $FIRST_ROW-$LAST_ROW" >&2
    return 1
  fi
}

[ "$MEASURE" -lt "$SECONDS_PER_STEP" ] || { echo "MEASURE must be shorter than the step" >&2; exit 1; }
[[ "$SENDERS" =~ ^[1-9][0-9]*$ ]] || { echo "SENDERS must be 1 or more" >&2; exit 1; }
PLAN=$(offsets)   # an assignment, so a refusal stops here
if [ "${1:-}" = plan ]; then
  echo "$PLAN" | while read -r rate row; do
    echo "$rate/s: rows $row +$(( rate * SECONDS_PER_STEP ))"
  done
  exit 0
fi

# Outside the tunnel: start again inside it (Prometheus on localhost:9090)
if [ "${RAMP_INNER:-}" != 1 ]; then
  export RAMP_INNER=1
  exec scripts/k8s_tunnel.sh service/prometheus=9090:9090 -- "$0" "$@"
fi

"$PY" -m load.ramp preflight
mkdir -p "$OUT"
rm -rf "${OUT:?}"/*   # this ramp's results only

# Scoring copies every 5 s (unix time, so a step's window can be matched)
echo "time,desired,current,ready" > "$OUT/copies.csv"
( while true; do
    hpa=$(kubectl -n fraud get hpa scoring-svc -o jsonpath='{.status.desiredReplicas},{.status.currentReplicas}' 2>/dev/null || echo ",")
    ready=$(kubectl -n fraud get deploy scoring-svc -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "")
    echo "$(date +%s),$hpa,$ready" >> "$OUT/copies.csv"
    sleep 5
  done ) &
WATCHER=$!
trap 'kill $WATCHER 2>/dev/null || true' EXIT

result=0
while read -r rate row; do
  dir=$OUT/$rate
  mkdir -p "$dir"
  echo "== $rate/s for $MINUTES min, rows $row +$(( rate * SECONDS_PER_STEP )), $SENDERS sender(s)"
  share=$("$PY" -c "print($rate / $SENDERS)")
  pids=()
  for i in $(seq 0 $(( SENDERS - 1 ))); do
    "$PY" -m load.replay --rps "$share" --offset "$row" --limit "$(( rate * SECONDS_PER_STEP ))" \
      --shard "$i/$SENDERS" --concurrency "$CONCURRENCY" --url "$URL" --out "$dir/run-$i.csv" \
      < /dev/null > "$dir/client-$i.txt" 2>&1 &
    pids+=($!)
  done
  failed=0
  for pid in "${pids[@]}"; do wait "$pid" || failed=1; done
  [ "$failed" = 0 ] || { echo "a sender failed: $dir/client-*.txt" >&2; result=1; break; }
  # The step starts with its first sender
  started=$(sed -n 's/^Started sending at unix //p' "$dir"/client-*.txt | sort -n | head -1)
  [ -n "$started" ] || { echo "no start time in $dir/client-*.txt" >&2; result=1; break; }
  # The measured window: the step's last MEASURE seconds, as scheduled from its first send
  end=$("$PY" -c "print(int(round($started + $SECONDS_PER_STEP)))")
  measure_start=$(( end - MEASURE ))
  echo "{\"start\": $started, \"measure_start\": $measure_start, \"end\": $end}" > "$dir/step.json"
  # Prometheus scrapes every 5 s: wait for the samples just after the window
  wait_s=$(( end + 10 - $(date +%s) )); [ "$wait_s" -gt 0 ] && sleep "$wait_s"
  "$PY" -m observability.snapshot --seconds "$MEASURE" --end "$end" --json "$dir/prometheus.json" \
    < /dev/null > "$dir/prometheus.md"
  verdict=0
  "$PY" -m load.ramp judge "$dir" --rps "$rate" --seconds "$SECONDS_PER_STEP" --measure "$MEASURE" \
    --copies "$OUT/copies.csv" < /dev/null || verdict=$?
  if [ "$verdict" != 0 ]; then
    echo "stopping: the $rate/s step $([ "$verdict" = 2 ] && echo "wasn't reached by the sender" || echo "missed a target")"
    break
  fi
done <<< "$PLAN"

"$PY" -m load.ramp report "$OUT" | tee "$OUT/summary.md"
echo "Saved: $OUT/summary.md"
exit $result
