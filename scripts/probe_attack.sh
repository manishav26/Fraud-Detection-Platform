#!/usr/bin/env bash
# E6 live attack on the cluster (week 4, step 7c). docs/scoring.md, "Probe detection"
#
#   scripts/probe_attack.sh            make k8s-probe-attack           probe detection on
#   D3=off scripts/probe_attack.sh     make k8s-probe-attack D3=off    off for the run, then on
#
# D3=off overrides scoring-svc's PROBE_DETECTION for the run (a rolling restart, no errors) and
# removes the override at the end, even if the run fails or is stopped, so deploy/scoring.env's
# "on" applies again.
set -euo pipefail
cd "$(dirname "$0")/.."
NS=fraud
D3=${D3:-on}
DEPLOY=deployment/scoring-svc

if [ "$D3" = off ]; then
  restore() {
    echo "probe detection back on"
    kubectl -n $NS set env $DEPLOY PROBE_DETECTION- >/dev/null
    kubectl -n $NS rollout status $DEPLOY --timeout=300s >/dev/null
  }
  trap restore EXIT
  # Ctrl+C or a kill: exit normally, so the EXIT trap above still switches it back on
  trap 'echo " (stopped)"; exit 130' INT TERM
  echo "switching probe detection off for this run"
  kubectl -n $NS set env $DEPLOY PROBE_DETECTION=off >/dev/null
  kubectl -n $NS rollout status $DEPLOY --timeout=300s
fi
python -m load.probe_attack --url http://localhost:8080 --d3 "$D3" "$@"
