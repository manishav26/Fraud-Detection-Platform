#!/usr/bin/env bash
# The feature job's parity and throughput runs on the cluster (week 4, step 1). docs/spark.md
#
#   scripts/feature_job_run.sh parity [spark|python]       make k8s-spark-parity
#   scripts/feature_job_run.sh throughput [spark|python]   make k8s-spark-throughput
#
# Each run uses its own topic (txn.parity, txn.bench), created empty, and its own new checkpoint
# folder, so it starts with empty state and isn't mixed up with what txn.raw already holds. The
# job is pointed at it, then put back on txn.raw and its normal checkpoint at the end, even if the
# run fails. Payments are written straight into the topic (stream/load_raw.py: skips Kong and
# ingest), as the Redpanda user `tools`, which may write only these two topics (docs/kafka.md);
# the job writes features_by_txn as usual.
set -euo pipefail
cd "$(dirname "$0")/.."
NS=fraud
WHAT=${1:?usage: scripts/feature_job_run.sh parity|throughput [spark|python]}
JOB=${2:-spark}
N=${N:-$([ "$WHAT" = parity ] && echo 10000 || echo 100000)}
RUN=$(date +%s)
case "$WHAT" in
  parity) TOPIC=txn.parity; PREFIX="" ;;
  throughput) TOPIC=txn.bench; PREFIX="bench$RUN-" ;;
  *) echo "parity or throughput" >&2; exit 1 ;;
esac
STS=statefulset/feature-job

payments() {   # how many payments the job has handled since its pod started (its metrics);
  # 0 if it can't be read right now (pod restarting, exec hiccup)
  local n
  n=$(kubectl -n $NS exec feature-job-0 -c feature-job -- python -c '
import urllib.request
text = urllib.request.urlopen("http://localhost:8000/metrics", timeout=5).read().decode()
print(int(sum(float(l.split()[-1]) for l in text.splitlines()
              if l.startswith("feature_job_payments_total{"))))' 2>/dev/null) || true
  [[ "$n" =~ ^[0-9]+$ ]] && echo "$n" || echo 0
}

restore() {
  echo "putting the job back on txn.raw and its normal checkpoint"
  kubectl -n $NS set env $STS JOB=spark TOPIC=txn.raw CHECKPOINT=/checkpoint/feature-job \
    STARTING_OFFSETS=earliest >/dev/null
  kubectl -n $NS rollout status $STS --timeout=300s >/dev/null
  kubectl -n $NS exec feature-job-0 -c feature-job -- sh -c "rm -rf /checkpoint/run-$RUN" || true
}

echo "1. a fresh topic $TOPIC (6 partitions, like txn.raw)"
./scripts/rpk.sh topic delete "$TOPIC" </dev/null >/dev/null 2>&1 || true
./scripts/rpk.sh topic create "$TOPIC" -p 6 -r 1 </dev/null

trap restore EXIT
# Ctrl+C or a kill: exit normally, so the EXIT trap above still puts the job back
trap 'echo " (stopped)"; exit 130' INT TERM
if [ "$WHAT" = throughput ]; then
  # A backlog: load first, then start the job, so it reads as fast as it can
  echo "2. stop the job, write $N payments into $TOPIC"
  kubectl -n $NS scale $STS --replicas=0
  kubectl -n $NS wait --for=delete pod/feature-job-0 --timeout=120s || true
  ./scripts/k8s_secrets.sh run ./scripts/k8s_tunnel.sh pod/redpanda-0=9092:9092 -- \
    python -m stream.load_raw --topic "$TOPIC" --limit "$N" --prefix "$PREFIX"
  echo "3. start the $JOB job on $TOPIC with empty state"
  kubectl -n $NS set env $STS JOB="$JOB" TOPIC="$TOPIC" CHECKPOINT="/checkpoint/run-$RUN" \
    STARTING_OFFSETS=earliest >/dev/null
  kubectl -n $NS scale $STS --replicas=1
  kubectl -n $NS rollout status $STS --timeout=300s
else
  echo "2. start the $JOB job on $TOPIC with empty state"
  kubectl -n $NS set env $STS JOB="$JOB" TOPIC="$TOPIC" CHECKPOINT="/checkpoint/run-$RUN" \
    STARTING_OFFSETS=earliest >/dev/null
  kubectl -n $NS rollout status $STS --timeout=300s
  echo "3. write the first $N stream payments into $TOPIC"
  ./scripts/k8s_secrets.sh run ./scripts/k8s_tunnel.sh pod/redpanda-0=9092:9092 -- \
    python -m stream.load_raw --topic "$TOPIC" --limit "$N" --prefix "$PREFIX"
fi

echo "4. wait until the job has handled all $N"
FIRST_T="" FIRST_N=0 DONE_T=""
for _ in $(seq 900); do
  n=$(payments)
  now=$(python3 -c 'import time; print(time.time())')
  if [ -z "$FIRST_T" ] && [ "$n" -gt 0 ]; then FIRST_T=$now FIRST_N=$n; fi
  if [ "$n" -ge "$N" ]; then DONE_T=$now; break; fi
  sleep 1
done
[ -n "$DONE_T" ] || { echo "the job handled only $(payments) of $N in 15 min" >&2; exit 1; }

if [ "$WHAT" = throughput ]; then
  # From the end of the first batch to the last: the first batch includes Spark's start-up
  python3 -c "
n, t = $N - $FIRST_N, $DONE_T - $FIRST_T
print(f'| Job | Payments | Seconds | Payments per second |')
print(f'|---|---|---|---|')
print(f'| $JOB | {n:,} (after the first batch of $FIRST_N) | {t:.1f} | {n / t:,.0f} |' if t > 0 else
      f'| $JOB | all $N in the first batch | - | - |')"
else
  echo "5. compare with the offline training code"
  ./scripts/k8s_secrets.sh run ./scripts/k8s_tunnel.sh service/cassandra=9042:9042 -- \
    python -m stream.parity --limit "$N" --prefix "$PREFIX" --since "$RUN"
fi
