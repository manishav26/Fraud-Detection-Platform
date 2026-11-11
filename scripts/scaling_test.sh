#!/usr/bin/env bash
# Scaling test (week 5, on the VM): the same work with 1, 2 and 4 scoring copies.
# Design and results: docs/results/scaling.md, "Week 5: 1, 2 and 4 copies".
# Output: data/scaling/<copies>/ and data/scaling/summary.md (not in git).
#
#   scripts/scaling_test.sh            1, 2 and 4 copies, 18,000 real payments each
#   scripts/scaling_test.sh plan       just print each run's rows
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
CONCURRENCY=${CONCURRENCY:-64}    # in flight: enough that the platform, not the sender, sets the pace
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
restore() {
  IFS=, read -r min max <<< "$ORIG_HPA"
  kubectl -n "$NS" patch hpa scoring-svc -p "{\"spec\":{\"minReplicas\":${min:-1},\"maxReplicas\":$max}}" >/dev/null
  echo "HPA back to ${min:-1}-$max copies"
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
while read -r n row; do
  dir=$OUT/$n
  mkdir -p "$dir"
  echo "== $n scoring copies: $COUNT payments from row $row, $CONCURRENCY in flight"
  hold_at "$n"
  sleep 20   # new pods: let their first calls (connections, the model library) pass before measuring
  "$PY" -m load.replay --speed 0 --offset "$row" --limit "$COUNT" --concurrency "$CONCURRENCY" \
    --url "$URL" --out "$dir/run.csv" < /dev/null > "$dir/client.txt" 2>&1
  [ "$(ready)" = "$n" ] || echo "warning: $(ready) copies ready at the end, not $n" | tee -a "$dir/client.txt"
  started=$(sed -n 's/^Started sending at unix //p' "$dir/client.txt")
  wall=$(sed -n 's/.* payments in \([0-9.]*\) s.*/\1/p' "$dir/client.txt")
  end=$("$PY" -c "print(int(round($started + $wall)))")
  sleep 10   # the last Prometheus samples of the run
  "$PY" -m observability.snapshot --seconds "$("$PY" -c "print(max(30, int($wall)))")" --end "$end" \
    --json "$dir/prometheus.json" < /dev/null > "$dir/prometheus.md"
  tail -3 "$dir/client.txt"
done <<< "$PLAN"

"$PY" -m load.scaling report "$OUT" | tee "$OUT/summary.md"
echo "Saved: $OUT/summary.md"
