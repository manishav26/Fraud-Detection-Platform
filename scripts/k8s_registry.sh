#!/usr/bin/env bash
# Runs a command on the laptop with tunnels to the cluster's MLflow and Redpanda, then closes them.
# Nothing is exposed outside the cluster, and the promotion key stays on the laptop.
#   make k8s-register   make k8s-promote VERSION=1   make k8s-mlflow
# Same local ports as compose, because Redpanda tells laptop clients to use localhost:9092.
set -euo pipefail
cd "$(dirname "$0")/.."

NS=fraud
PIDS=()
cleanup() {
  for pid in "${PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT

for port in 5050 9092; do
  if nc -z localhost "$port" 2>/dev/null; then
    # Something else (usually compose) would answer, and we would change the wrong registry
    echo "Port $port is already in use, probably by compose. Run: make down" >&2
    exit 1
  fi
done

kubectl -n "$NS" port-forward service/mlflow 5050:5000 >/dev/null &
PIDS+=($!)
# To the pod: the Service has only the in-cluster port
kubectl -n "$NS" port-forward pod/redpanda-0 9092:9092 >/dev/null &
PIDS+=($!)

for port in 5050 9092; do
  for _ in $(seq 40); do
    nc -z localhost "$port" 2>/dev/null && break
    sleep 0.25
  done
  nc -z localhost "$port" 2>/dev/null || { echo "Tunnel to port $port did not open" >&2; exit 1; }
done

"$@"
