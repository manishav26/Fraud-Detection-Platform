#!/usr/bin/env bash
# ST5: Schemathesis fuzzes the OpenAPI contract through Kong (threat 5). docs/results/security.md
#
#   make k8s-st5            needs `pip install -r requirements-dev.txt` (Schemathesis)
#
# Each documented route, as a role allowed to call it, so requests reach the service rather than
# stopping at Kong's 403:
#   POST /v1/transactions/score   as load-test (full answer) and as merchant-1 (decision only)
#   GET  /v1/cases/txn/{txn_id}   as case-mgmt
# Left out: POST /v1/cases/txn/{txn_id}/decision. Fuzzed ids could match real open cases and
# resolve them; ST6 covers that route on a case of its own.
# Pass (the plan's rule): no 5xx and no crash (check not_a_server_error). CHECKS=all also checks
# every answer against the contract, to find gaps in docs/openapi.yaml.
# Side effect: the valid fuzzed payments are scored and stored like any other (pseudonymized).
set -euo pipefail
cd "$(dirname "$0")/.."
URL=${URL:-http://localhost:8080}
N=${N:-500}
CHECKS=${CHECKS:-not_a_server_error}
command -v st >/dev/null || { echo "Schemathesis is missing: pip install -r requirements-dev.txt" >&2; exit 1; }

fuzz() {   # fuzz <client> <max examples> <schemathesis filters...>
  local client=$1 n=$2; shift 2
  # A new token right before each part: payments need one under 5 minutes old (threat 6)
  local token
  token=$(python -m load.credentials "$client" --url "$URL")
  echo; echo "== as $client: $*"
  # On the command line for the run: a short-lived test token (payments take it for 5 minutes)
  st run docs/openapi.yaml --url "$URL" --header "Authorization: Bearer $token" \
    --checks "$CHECKS" --max-examples "$n" --workers 4 --request-timeout 15 \
    --report junit --report-dir "data/st5/$client" "$@"
}

status=0
fuzz load-test "$N" --include-path /v1/transactions/score || status=1
fuzz merchant-1 "$N" --include-path /v1/transactions/score || status=1
fuzz case-mgmt 200 --include-path /v1/cases/txn/{txn_id} --include-method GET || status=1
if [ $status -eq 0 ]; then echo; echo "ST5 PASSED: no 5xx and no crash"; else echo; echo "ST5 FAILED (see above)"; fi
exit $status
