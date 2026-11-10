#!/usr/bin/env bash
# Cluster passwords check (week 4, step 5 part B; threat 13). make k8s-secrets-check
# After `make k8s-up` with the encrypted passwords (docs/secrets.md):
#   1. no Secret in the namespace still holds a dev password written in this repo
#   2. each Postgres login accepts its new password and refuses its old dev one
# (Cassandra's two logins are checked the same way by make k8s-st4-db.)
# Passwords go to the pod on stdin, never on a command line. Changes nothing. Exit 1 on a failure.
set -uo pipefail
cd "$(dirname "$0")/../.."
# shellcheck source=scripts/k8s_secrets.sh
source scripts/k8s_secrets.sh
load_secrets || exit 1
NS=fraud
FAILED=0
row() {   # row <check> <expected> <got>
  local mark=""
  [ "$2" = "$3" ] || { mark=" WRONG"; FAILED=1; }
  printf '| %s | %s | %s%s |\n' "$1" "$2" "$3" "$mark"
}
printf '| Check | Expected | Got |\n|---|---|---|\n'

# 1. Every value of every Secret, decoded, searched for the dev passwords' common ending
DEV_LEFT=$(kubectl -n "$NS" get secrets -o json |
  python3 -c 'import base64, json, sys
for s in json.load(sys.stdin)["items"]:
    for k, v in (s.get("data") or {}).items():
        if "_dev_only" in base64.b64decode(v).decode(errors="replace"):
            print(s["metadata"]["name"] + "/" + k)')
row "Secrets holding a dev password" none "${DEV_LEFT:-none}"

# 2. Over TCP to the pod's own address: the local socket and 127.0.0.1 skip passwords (Postgres
#    image default), but every other pod connects through the address, where passwords count
login() {   # login <user> <password>: ok or refused
  if printf '%s\n' "$2" | kubectl -n "$NS" exec -i postgres-0 -c postgres -- sh -c \
      'read -r PGPASSWORD; export PGPASSWORD; psql -h "$(hostname -i)" -U "$0" -d postgres -Atc "SELECT 1"' \
      "$1" >/dev/null 2>&1; then echo ok; else echo refused; fi
}
for pair in "fraud:$POSTGRES_PASSWORD:fraud_dev_only" "mlflow:$MLFLOW_DB_PASSWORD:mlflow_dev_only" \
            "alerts:$ALERTS_DB_PASSWORD:alerts_dev_only" "model_admin:$MODEL_ADMIN_DB_PASSWORD:model_admin_dev_only"; do
  IFS=: read -r user new old <<< "$pair"
  row "Postgres $user, new password" ok "$(login "$user" "$new")"
  row "Postgres $user, old dev password" refused "$(login "$user" "$old")"
done

echo
if [ "$FAILED" -ne 0 ]; then echo "Secrets check FAILED"; exit 1; fi
echo "Secrets check PASSED: the cluster uses only the encrypted passwords"
