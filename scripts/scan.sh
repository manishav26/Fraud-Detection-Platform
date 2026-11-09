#!/usr/bin/env bash
# Supply-chain scans (week 4, step 5; threat 13, test ST10). The same three run in CI.
#   make scan              all three        scripts/scan.sh secrets|deps|config|images
# All three run in pinned Docker images: nothing to install on the laptop.
# Waivers, each with its reason: .gitleaks.toml, .trivyignore.yaml.
# Exit code 1 if any scan finds something not waived. Design: docs/supply-chain.md
set -uo pipefail
cd "$(dirname "$0")/.."

GITLEAKS=zricethezav/gitleaks:v8.30.1
TRIVY=aquasec/trivy:0.76.1
PYTHON_IMAGE=python:3.11-slim   # the same base as our services
PIP_AUDIT=2.10.1
IMAGES=(token-issuer ingest-svc scoring-svc mlflow alert-svc model-admin-svc siem-mock)
CACHE="${TRIVY_CACHE:-$HOME/.cache/trivy}"   # its vulnerability database, kept between runs
mkdir -p "$CACHE"
WHAT=${1:-all}
FAILED=()

run() {   # run <name> <command...>
  echo; echo "== $1"
  shift
  if "$@"; then echo "-- passed"; else echo "-- FAILED"; FAILED+=("$NAME"); fi
}

secrets() {   # every commit in the history, not just the files as they are now
  docker run --rm -v "$PWD:/repo" "$GITLEAKS" git /repo --config /repo/.gitleaks.toml \
    --redact --no-banner
}

deps() {   # our pinned versions against known vulnerabilities (PyPI's advisory database)
  # --no-deps: exactly the versions we pin. What they pull in is checked in the images (below).
  docker run --rm -v "$PWD:/src:ro" -w /src "$PYTHON_IMAGE" sh -c '
    pip install -q --disable-pip-version-check "pip-audit==$0" 2>/dev/null || exit 2
    status=0
    for f in requirements-dev.txt services/*/requirements.txt; do
      echo "-- $f"
      out=$(python -m pip_audit -r "$f" --no-deps --disable-pip --progress-spinner off 2>&1) \
        || status=1
      echo "$out" | grep -v "^WARNING"   # the "consider hashes" advice, on every file
    done
    exit $status' "$PIP_AUDIT"
}

trivy() { docker run --rm -v "$CACHE:/root/.cache/trivy" "$@"; }

config() {   # Dockerfiles, Kubernetes YAML, compose: risky settings
  trivy -v "$PWD:/repo" "$TRIVY" config --severity HIGH,CRITICAL --exit-code 1 \
    --ignorefile /repo/.trivyignore.yaml /repo
}

images() {   # what is really installed in our images: OS packages and Python packages
  local img status=0
  for img in "${IMAGES[@]}"; do
    echo "-- fraud/$img:dev"
    trivy -v /var/run/docker.sock:/var/run/docker.sock -v "$PWD/.trivyignore.yaml:/.trivyignore.yaml:ro" \
      "$TRIVY" image --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 \
      --ignorefile /.trivyignore.yaml "fraud/$img:dev" || status=1
  done
  return $status
}

for NAME in secrets deps config images; do
  if [ "$WHAT" = all ] || [ "$WHAT" = "$NAME" ]; then
    run "$NAME" "$NAME"
  fi
done

echo
if [ ${#FAILED[@]} -gt 0 ]; then
  echo "SCAN FAILED: ${FAILED[*]}"; exit 1
fi
echo "SCAN PASSED"
