#!/usr/bin/env bash
# Makes the cluster's passwords and writes them encrypted to deploy/k8s/secrets.enc.env (SOPS
# with age, for the public key in .sops.yaml). Design: docs/secrets.md
#   make k8s-secrets              add any password that's missing; keep the others
#   make k8s-secrets ROTATE=1     new values for all of them
# Then `make k8s-up` puts them into the cluster and changes the logins to match.
# The plain values are only ever in memory and a private temporary folder, deleted on exit.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/k8s_secrets.sh
source scripts/k8s_secrets.sh
SOPS_CONFIG=${SOPS_CONFIG:-.sops.yaml}
ROTATE=${ROTATE:-}

command -v sops >/dev/null || { echo "sops is missing: brew install sops age" >&2; exit 1; }
grep -q "age1" "$SOPS_CONFIG" || { echo "No age public key in $SOPS_CONFIG" >&2; exit 1; }

umask 077
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

for name in "${SECRET_NAMES[@]}"; do unset "$name"; done   # only values from the file count
if [ -f "$SECRETS_FILE" ] && [ -z "$ROTATE" ]; then
  load_secrets 2>/dev/null || true   # keep what's there; anything missing is made below
fi

ADDED=()
for name in "${SECRET_NAMES[@]}"; do
  if [ -n "$ROTATE" ] || [ -z "${!name:-}" ]; then
    printf -v "$name" '%s' "$(openssl rand -hex 24)"   # 192 random bits, letters and digits only
    ADDED+=("$name")
  fi
  echo "$name=${!name}" >> "$TMP/plain.env"
done

if [ ${#ADDED[@]} -eq 0 ]; then
  echo "$SECRETS_FILE has every password; nothing changed (new values: make k8s-secrets ROTATE=1)"
  exit 0
fi

sops --config "$SOPS_CONFIG" --encrypt --filename-override "$SECRETS_FILE" \
  --input-type dotenv --output-type dotenv "$TMP/plain.env" > "$TMP/encrypted.env"
mv "$TMP/encrypted.env" "$SECRETS_FILE"
echo "wrote $SECRETS_FILE (encrypted); new: ${ADDED[*]}"
echo "Next: make k8s-up (puts them into the cluster and changes the logins), then commit the file"
