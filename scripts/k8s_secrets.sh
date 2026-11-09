#!/usr/bin/env bash
# The cluster's passwords (week 4, step 5 part B; threat 13). Design: docs/secrets.md
# They live in git only encrypted (deploy/k8s/secrets.enc.env, SOPS with age); the key that opens
# them is on the laptop only (~/.config/sops/age/keys.txt). Compose keeps its dev passwords.
#
#   scripts/k8s_secrets.sh run <command...>   run a laptop tool with the passwords it needs
#   scripts/k8s_secrets.sh show <NAME>        print one (e.g. GRAFANA_ADMIN_PASSWORD)
#   source scripts/k8s_secrets.sh; load_secrets   (scripts/k8s_up.sh, scripts/gen_secrets.sh)
#
# Decrypted values exist only in memory and in the cluster's Secrets, never in a file.

SECRETS_FILE=${SECRETS_FILE:-deploy/k8s/secrets.enc.env}
# Every password the cluster uses. Adding one: add it here, run `make k8s-secrets`, use it in
# scripts/k8s_up.sh.
SECRET_NAMES=(
  POSTGRES_PASSWORD            # Postgres superuser `fraud`: schema, the laptop's audit check
  MLFLOW_DB_PASSWORD           # Postgres `mlflow`: MLflow's own database
  ALERTS_DB_PASSWORD           # Postgres `alerts`: alert-svc
  MODEL_ADMIN_DB_PASSWORD      # Postgres `model_admin`: model-admin-svc
  CASSANDRA_ADMIN_PASSWORD     # Cassandra `fraud_admin`: schema, backfill, read timing
  CASSANDRA_SCORING_PASSWORD   # Cassandra `scoring`: scoring-svc
  OBJECTSTORE_SECRET_KEY       # RustFS: MLflow's model files
  GRAFANA_ADMIN_PASSWORD       # Grafana `admin`
)
# sops on macOS looks in ~/Library/Application Support by default; we keep the key here instead
export SOPS_AGE_KEY_FILE="${SOPS_AGE_KEY_FILE:-$HOME/.config/sops/age/keys.txt}"

decrypt_secrets() {   # prints NAME=value lines
  command -v sops >/dev/null || { echo "sops is missing: brew install sops age" >&2; return 1; }
  [ -f "$SECRETS_FILE" ] || { echo "No $SECRETS_FILE yet: make k8s-secrets" >&2; return 1; }
  sops --decrypt --input-type dotenv --output-type dotenv "$SECRETS_FILE" || {
    echo "Could not decrypt $SECRETS_FILE: is your age key in $SOPS_AGE_KEY_FILE?" >&2; return 1; }
}

load_secrets() {   # exports each name in SECRET_NAMES
  local plain key value name
  plain=$(decrypt_secrets) || return 1
  while IFS='=' read -r key value; do
    [[ " ${SECRET_NAMES[*]} " == *" $key "* ]] && export "$key=$value"
  done <<< "$plain"
  for name in "${SECRET_NAMES[@]}"; do
    [ -n "${!name:-}" ] || { echo "$name is missing from $SECRETS_FILE: make k8s-secrets" >&2; return 1; }
  done
}

# Run directly (not sourced)
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  set -euo pipefail
  cd "$(dirname "$0")/.."
  case "${1:-}" in
    run)
      shift
      load_secrets
      # What the laptop tools read: store/history.py (backfill, read timing) logs in as the admin,
      # audit/verify.py uses the database owner; both through a tunnel on localhost
      export CASSANDRA_USER=fraud_admin CASSANDRA_PASSWORD="$CASSANDRA_ADMIN_PASSWORD"
      export AUDIT_DSN="postgresql://fraud:$POSTGRES_PASSWORD@localhost:5432/fraud"
      exec "$@"
      ;;
    show)
      load_secrets
      name=${2:?usage: scripts/k8s_secrets.sh show <NAME>}
      [[ " ${SECRET_NAMES[*]} " == *" $name "* ]] || { echo "unknown: $name (one of: ${SECRET_NAMES[*]})" >&2; exit 1; }
      echo "${!name}"
      ;;
    *)
      echo "usage: scripts/k8s_secrets.sh run <command...> | show <NAME>" >&2; exit 1 ;;
  esac
fi
