#!/usr/bin/env bash
# Generates a dev RS256 keypair and renders deploy/kong/kong.yml. Keys are gitignored.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p keys
if [ ! -f keys/private.pem ]; then
  openssl genrsa -out keys/private.pem 2048 2>/dev/null
  openssl rsa -in keys/private.pem -pubout -out keys/public.pem 2>/dev/null
fi
python3 - <<'PY'
pub = open("keys/public.pem").read().rstrip().splitlines()
indented = "\n".join("          " + l for l in pub)
tpl = open("deploy/kong/kong.tpl.yml").read()
open("deploy/kong/kong.yml", "w").write(tpl.replace("@@PUBLIC_KEY@@", indented))
PY
