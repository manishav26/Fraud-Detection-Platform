#!/usr/bin/env bash
# Scale and resilience tests on the k3d cluster (week 4, step 2). Each sends real payments through
# Kong while something happens to the pods, and records the client's answers plus the scoring
# copies every 5 s. Results: docs/results/scaling.md. Output: data/resilience/<test>/ (not in git).
#   scripts/resilience_test.sh scale     80/s for 6 min: the HPA adds copies; then 6 min idle
#                                        to watch it remove them again
#   scripts/resilience_test.sh kill      30/s for 3 min; delete a scoring pod at 60 s, an ingest
#                                        pod at 120 s
#   scripts/resilience_test.sh rollout   30/s for 3 min; restart scoring at 60 s (new pods one by one)
#   scripts/resilience_test.sh drain     30/s for 3 min; drain a node's scoring, ingest and alert
#                                        pods at 60 s (the PodDisruptionBudgets apply), then uncordon
# RPS and MINUTES can be changed. Each test uses its own rows (after the baseline's 0-159,999), so
# no payment is sent twice. Events happen at a third (and two thirds) of the run.
#   RERUN=N scripts/resilience_test.sh kill|drain    another run on rows not sent before: N=1
#                                                    is 2 minutes, N=2..6 are 3 minutes
set -euo pipefail
cd "$(dirname "$0")/.."

TEST=${1:-}
PY=${PYTHON:-python}
NS=fraud
URL=http://localhost:8080
case "$TEST" in
  scale)   OFFSET=160000; RPS=${RPS:-80}; MINUTES=${MINUTES:-6}; AFTER=360 ;;
  kill)    OFFSET=185000; RPS=${RPS:-30}; MINUTES=${MINUTES:-3}; AFTER=20 ;;
  rollout) OFFSET=191000; RPS=${RPS:-30}; MINUTES=${MINUTES:-3}; AFTER=20 ;;
  drain)   OFFSET=197000; RPS=${RPS:-30}; MINUTES=${MINUTES:-3}; AFTER=20 ;;
  *) echo "usage: scripts/resilience_test.sh scale|kill|rollout|drain" >&2; exit 1 ;;
esac
OUT=data/resilience/$TEST
if [ -n "${RERUN:-}" ]; then
  # RERUN=1: rows 203,000-210,439, 2 minutes each. RERUN=2..6: rows 100,000-159,999, which only
  # compose's baseline used (never this cluster), 3 minutes each, kill and drain per block.
  [ "$TEST" = kill ] || [ "$TEST" = drain ] || {
    echo "RERUN is for kill and drain only" >&2; exit 1; }
  if [ "$RERUN" = 1 ]; then
    MINUTES=2; [ "$TEST" = kill ] && OFFSET=203000 || OFFSET=206600
    OUT=data/resilience/$TEST-rerun
  elif [ "$RERUN" -ge 2 ] && [ "$RERUN" -le 6 ]; then
    MINUTES=3; OFFSET=$(( 100000 + (RERUN - 2) * 10800 ))
    [ "$TEST" = drain ] && OFFSET=$(( OFFSET + 5400 ))
    OUT=data/resilience/$TEST-rerun$RERUN
  else
    echo "RERUN must be 1 to 6" >&2; exit 1
  fi
  RPS=30
fi
COUNT=$(( RPS * 60 * MINUTES ))
FIRST=$(( MINUTES * 20 ))    # seconds: a third of the run
SECOND=$(( MINUTES * 20 ))   # and another third later
mkdir -p "$OUT"
EVENTS=$OUT/events.txt
: > "$EVENTS"
note() { echo "$(date '+%H:%M:%S') $*" | tee -a "$EVENTS"; }

# Scoring copies every 5 s: what the HPA wants, how many exist, how many are ready, CPU vs target
watch_copies() {
  echo "time,desired,current,ready,cpu_percent_of_request" > "$OUT/copies.csv"
  while true; do
    local hpa ready
    hpa=$(kubectl -n "$NS" get hpa scoring-svc -o \
      jsonpath='{.status.desiredReplicas},{.status.currentReplicas},{.status.currentMetrics[0].resource.current.averageUtilization}' \
      2>/dev/null || echo ",,")
    ready=$(kubectl -n "$NS" get deploy scoring-svc -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "")
    IFS=, read -r desired current cpu <<< "$hpa"
    echo "$(date '+%H:%M:%S'),$desired,$current,$ready,$cpu" >> "$OUT/copies.csv"
    sleep 5
  done
}

watch_copies &
WATCHER=$!
trap 'kill $WATCHER 2>/dev/null || true' EXIT

note "start: $TEST, $RPS/s for $MINUTES min, $COUNT payments from row $OFFSET"
"$PY" -m load.replay --rps "$RPS" --offset "$OFFSET" --limit "$COUNT" --url "$URL" \
  --out "$OUT/run.csv" > "$OUT/client.txt" 2>&1 &
REPLAY=$!

first_pod() { kubectl -n "$NS" get pods -l "app=$1" -o jsonpath='{.items[0].metadata.name}'; }
case "$TEST" in
  kill)
    sleep "$FIRST"
    pod=$(first_pod scoring-svc); note "delete scoring pod $pod"
    kubectl -n "$NS" delete pod "$pod" --wait=false
    sleep "$SECOND"
    pod=$(first_pod ingest-svc); note "delete ingest pod $pod"
    kubectl -n "$NS" delete pod "$pod" --wait=false
    ;;
  rollout)
    sleep "$FIRST"
    note "rollout restart scoring-svc"
    kubectl -n "$NS" rollout restart deploy/scoring-svc
    kubectl -n "$NS" rollout status deploy/scoring-svc --timeout=300s
    note "rollout finished"
    ;;
  drain)
    sleep "$FIRST"
    node=$(kubectl -n "$NS" get pod "$(first_pod scoring-svc)" -o jsonpath='{.spec.nodeName}')
    note "drain $node (scoring, ingest and alert pods only; data stores stay)"
    kubectl drain "$node" --pod-selector='app in (scoring-svc,ingest-svc,alert-svc)' \
      --ignore-daemonsets --delete-emptydir-data --timeout=180s 2>&1 | tee -a "$EVENTS"
    note "drain finished"
    ;;
esac

wait "$REPLAY" || true
note "load finished; watching copies for $AFTER s more"
if [ "$TEST" = drain ]; then
  kubectl uncordon "$node" && note "uncordoned $node"
fi
sleep "$AFTER"

{
  echo "# Resilience test: $TEST, $RPS/s for $MINUTES min ($(date '+%Y-%m-%d'))"
  echo
  echo "## Client (through Kong)"
  echo '```'
  cat "$OUT/client.txt"
  echo '```'
  echo
  echo "## What happened"
  echo '```'
  cat "$EVENTS"
  echo '```'
  echo
  echo "## Scoring copies (every 5 s)"
  echo
  "$PY" - "$OUT/copies.csv" <<'EOF'
import csv, sys
rows = [r for r in csv.DictReader(open(sys.argv[1])) if r["ready"]]
def num(v):
    try:
        return int(v)
    except ValueError:
        return None
most = max((num(r["ready"]) or 0) for r in rows) if rows else 0
first_up = next((r["time"] for r in rows if (num(r["desired"]) or 0) > 2), None)
first_ready = next((r["time"] for r in rows if (num(r["ready"]) or 0) > 2), None)
print(f"- most copies ready: {most}")
print(f"- HPA first asked for more than 2: {first_up or 'never'}")
print(f"- first time more than 2 were ready: {first_ready or 'never'}")
print(f"- samples: {len(rows)} (all in copies.csv)")
EOF
} | tee "$OUT/summary.md"
echo "Saved: $OUT/summary.md"
