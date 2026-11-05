#!/usr/bin/env bash
# One baseline run (week 3, step 8): real payments at a fixed rate, then that window's numbers.
#   scripts/baseline.sh k8s 1          warm run 1 of 3 on the cluster (also 2, 3)
#   scripts/baseline.sh k8s fresh      restart ingest and scoring, then start with no warm-up
#   scripts/baseline.sh compose 1      warm run 1 of 3 on compose (also 2, 3)
# RPS (default 30) and MINUTES (default 10) can be changed. Results: docs/results/baseline.md
# Output goes to data/baseline/<where>-<run>/ (not in git).
set -euo pipefail
cd "$(dirname "$0")/.."

WHERE=${1:-}
RUN=${2:-}
RPS=${RPS:-30}
MINUTES=${MINUTES:-10}
PY=${PYTHON:-python}

# Each run gets its own 20,000 payments, so no txn_id is ever sent twice (a repeat would put a
# user's later payments into their history). Rows 0-19,999 were used by earlier replays.
case "$WHERE:$RUN" in
  k8s:1) SLOT=1 ;; k8s:2) SLOT=2 ;; k8s:3) SLOT=3 ;; k8s:fresh) SLOT=4 ;;
  compose:1) SLOT=5 ;; compose:2) SLOT=6 ;; compose:3) SLOT=7 ;;
  *) echo "usage: scripts/baseline.sh k8s 1|2|3|fresh   or   scripts/baseline.sh compose 1|2|3" >&2
     exit 1 ;;
esac
OFFSET=$((SLOT * 20000))
WARMUP=$((RPS * 60))
COUNT=$((RPS * 60 * MINUTES))
if [ $((WARMUP + COUNT)) -gt 20000 ]; then
  echo "RPS x (MINUTES + 1) x 60 must stay under 20,000 payments per run" >&2
  exit 1
fi

if [ "$WHERE" = k8s ]; then
  URL=http://localhost:8080
  snapshot() { ./scripts/k8s_tunnel.sh service/prometheus=9090:9090 -- \
                 "$PY" -m observability.snapshot "$@"; }
  pauses() { kubectl -n fraud logs cassandra-0 --since="$1m" | grep GCInspector || echo "none"; }
else
  URL=http://localhost:8000
  snapshot() { "$PY" -m observability.snapshot "$@"; }
  pauses() { docker compose logs --no-log-prefix --since "$1m" cassandra | grep GCInspector \
               || echo "none"; }
fi

OUT=data/baseline/$WHERE-$RUN
mkdir -p "$OUT"
replay() {   # replay <offset> <count> <name>
  "$PY" -m load.replay --rps "$RPS" --offset "$1" --limit "$2" --url "$URL" --out "$OUT/$3.csv"
}

if [ "$RUN" = fresh ]; then
  echo "== restarting ingest and scoring; no warm-up"
  kubectl -n fraud rollout restart deploy/ingest-svc deploy/scoring-svc
  kubectl -n fraud rollout status deploy/ingest-svc --timeout=300s
  kubectl -n fraud rollout status deploy/scoring-svc --timeout=300s
else
  echo "== warm-up: 1 minute at $RPS/s (not counted)"
  replay "$OFFSET" "$WARMUP" warmup > "$OUT/warmup.txt"
fi

echo "== run: $MINUTES minutes at $RPS/s, $COUNT payments from row $((OFFSET + WARMUP))"
START=$(date +%s)
replay $((OFFSET + WARMUP)) "$COUNT" run | tee "$OUT/client.txt"
END=$(date +%s)
sleep 10   # Prometheus reads every 5 s: let it pick up the last answers
# The run's exact length in seconds, so the numbers leave out the warm-up that ended just before.
# It includes the few seconds the replay spends loading, when nothing is sent.
WINDOW=$((END - START))

{
  echo "# $WHERE run $RUN, $RPS/s, started $(date -r "$START" '+%Y-%m-%d %H:%M:%S')"
  echo
  echo "## Client (through Kong)"
  echo '```'
  cat "$OUT/client.txt"
  echo '```'
  echo
  echo "## Server (Prometheus)"
  echo
  snapshot --seconds "$WINDOW" --end "$END"
  if [ "$RUN" = fresh ]; then
    echo
    echo "## First minute only"
    echo
    snapshot --seconds 60 --end $((START + 60))
  fi
  echo
  echo "## Cassandra pauses during the run"
  echo '```'
  pauses $(( (WINDOW + 59) / 60 + 1 ))   # logs only filter by whole minutes; a little extra
  echo '```'
} | tee "$OUT/summary.md"
echo "Saved: $OUT/summary.md"
