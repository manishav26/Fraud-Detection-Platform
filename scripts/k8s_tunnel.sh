#!/usr/bin/env bash
# Runs a command on the laptop with tunnels into the cluster, then closes them. Nothing is exposed
# outside the cluster, and the promotion key stays on the laptop.
#   scripts/k8s_tunnel.sh service/mlflow=5050:5000 pod/redpanda-0=9092:9092 -- python -m ...
# Used by make k8s-register, k8s-promote and k8s-backfill. Same local ports as compose, because
# Redpanda tells laptop clients to use localhost:9092 and our tools default to those ports.
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

TUNNELS=()
while [ "$1" != "--" ]; do
  TUNNELS+=("$1")
  shift
done
shift

# A tunnel is <target>=<local port>:<remote port>, or <target>=<local address>:<local port>:<remote
# port> to listen on another loopback address (127.0.0.2 ...; Linux only): the consistency test
# reaches each Cassandra pod on port 9042 of its own address (scripts/consistency_test.sh).
local_addr() {
  local ports="${1#*=}"
  if [ "$(echo "$ports" | tr -cd : | wc -c | tr -d ' ')" = 2 ]; then echo "${ports%%:*}"; else echo localhost; fi
}

local_port() {
  local ports="${1#*=}"
  [ "$(local_addr "$1")" = localhost ] || ports="${ports#*:}"
  echo "${ports%%:*}"
}

remote_ports() {   # what kubectl port-forward takes: <local port>:<remote port>
  local ports="${1#*=}"
  [ "$(local_addr "$1")" = localhost ] || ports="${ports#*:}"
  echo "$ports"
}

for tunnel in "${TUNNELS[@]}"; do
  port=$(local_port "$tunnel"); addr=$(local_addr "$tunnel")
  if nc -z "$addr" "$port" 2>/dev/null; then
    # Something else (usually compose) would answer, and we would change the wrong system
    echo "$addr:$port is already in use, probably by compose. Run: make down" >&2
    exit 1
  fi
done

for tunnel in "${TUNNELS[@]}"; do
  kubectl -n "$NS" port-forward --address "$(local_addr "$tunnel")" "${tunnel%%=*}" \
    "$(remote_ports "$tunnel")" >/dev/null &
  PIDS+=($!)
done

for tunnel in "${TUNNELS[@]}"; do
  port=$(local_port "$tunnel"); addr=$(local_addr "$tunnel")
  for _ in $(seq 40); do
    nc -z "$addr" "$port" 2>/dev/null && break
    sleep 0.25
  done
  nc -z "$addr" "$port" 2>/dev/null || { echo "Tunnel to $addr:$port did not open" >&2; exit 1; }
done

"$@"
