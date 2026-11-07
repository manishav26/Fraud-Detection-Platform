#!/usr/bin/env bash
# Generates a dev RS256 keypair and a secret for each client in deploy/clients.json, and renders
# deploy/kong/kong.yml with one Kong consumer per client. Keys and secrets are gitignored.
# Safe to run again: existing keys and secrets are kept. Design: docs/access.md
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p keys
if [ ! -f keys/private.pem ]; then
  openssl genrsa -out keys/private.pem 2048 2>/dev/null
  openssl rsa -in keys/private.pem -pubout -out keys/public.pem 2>/dev/null
fi
# Promotion signing key: a separate pair, in its own folder, because keys/ is mounted into the
# token issuer and no container may ever hold this private key. Design: docs/mlflow.md
mkdir -p promotion-keys
if [ ! -f promotion-keys/private.pem ]; then
  openssl genrsa -out promotion-keys/private.pem 2048 2>/dev/null
  chmod 600 promotion-keys/private.pem
  openssl rsa -in promotion-keys/private.pem -pubout -out promotion-keys/public.pem 2>/dev/null
fi
python3 - <<'PY'
import hashlib
import json
import os
import secrets

roster = {k: v for k, v in json.load(open("deploy/clients.json")).items() if not k.startswith("_")}

# keys/clients.json: plain secrets, for tools on the laptop only (make token, replay, load).
path = "keys/clients.json"
clients = json.load(open(path)) if os.path.exists(path) else {}
clients = {cid: {"role": role,
                 "secret": clients.get(cid, {}).get("secret") or secrets.token_urlsafe(32)}
           for cid, role in roster.items()}
with open(path, "w") as f:
    json.dump(clients, f, indent=2)
os.chmod(path, 0o600)

# keys/clients.issuer.json: only a SHA-256 of each secret, for the token issuer. The secrets are
# 32 random bytes, so a fast hash is enough (a slow one is for guessable human passwords).
issuer = {cid: {"role": c["role"], "secret_sha256": hashlib.sha256(c["secret"].encode()).hexdigest()}
          for cid, c in clients.items()}
with open("keys/clients.issuer.json", "w") as f:
    json.dump(issuer, f, indent=2)

# One Kong consumer per client. Kong finds it from the token's `iss` claim (= the client id) and
# checks the signature with the issuer's public key. The group is the client's role.
pub = open("keys/public.pem").read().rstrip().splitlines()
indented = "\n".join("          " + line for line in pub)
consumers = "".join(
    f"  - username: {cid}\n"
    f"    acls:\n      - group: {c['role']}\n"
    f"    jwt_secrets:\n      - key: {cid}\n        algorithm: RS256\n"
    f"        rsa_public_key: |\n{indented}\n"
    for cid, c in clients.items())
tpl = open("deploy/kong/kong.tpl.yml").read()
open("deploy/kong/kong.yml", "w").write(tpl.replace("@@CONSUMERS@@\n", consumers))
PY
