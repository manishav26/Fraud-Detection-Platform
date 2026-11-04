"""ST1: 1,000 payments with bad tokens. Every one must be refused, and none may reach ingest.

    make st1                 (compose)        make k8s-st1   (cluster)

Six kinds of bad token, each sent with a real-looking payment:
  forged         signed with a different private key
  expired        signed with the real key, but an hour old
  altered        a real token with its role changed to admin (signature no longer matches)
  alg-none       no signature at all ("alg": "none")
  alg-confusion  HMAC-signed using the issuer's PUBLIC key as the secret
  unknown-client real key, but `iss` names a client that doesn't exist

"None reached ingest": txn.raw's message count is read before and after, and must not change.
Run it with no other traffic. Prints a Markdown table for docs/results/security.md.
"""

import argparse
import base64
import hashlib
import hmac
import json
import re
import shlex
import subprocess
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from load.credentials import credentials

TOTAL = 1000
SCORE = "/v1/transactions/score"


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unsigned(header, claims, signature=b""):
    head = b64(json.dumps(header).encode()) + "." + b64(json.dumps(claims).encode())
    return head, head + "." + b64(signature)


def claims(client="merchant-1", role="client", age=0):
    now = int(time.time()) - age
    return {"iss": client, "sub": client, "role": role, "iat": now, "exp": now + 900}


def make_tokens(real_private, real_public, real_token):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    header, payload, signature = real_token.split(".")
    altered_claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    altered_claims["role"] = "admin"
    head, _ = unsigned({"alg": "HS256", "typ": "JWT"}, claims())
    confusion = head + "." + b64(hmac.new(real_public, head.encode(), hashlib.sha256).digest())
    return {
        "forged": jwt.encode(claims(), other, algorithm="RS256"),
        "expired": jwt.encode(claims(age=3600), real_private, algorithm="RS256"),
        "altered": f"{header}.{b64(json.dumps(altered_claims).encode())}.{signature}",
        "alg-none": unsigned({"alg": "none", "typ": "JWT"}, claims())[1],
        "alg-confusion": confusion,
        "unknown-client": jwt.encode(claims(client="nobody"), real_private, algorithm="RS256"),
    }


def raw_count(rpk):
    out = subprocess.run(shlex.split(rpk) + ["topic", "describe", "txn.raw", "-p"],
                         capture_output=True, text=True, check=True).stdout
    return sum(int(line.split()[-1]) for line in out.splitlines()[1:] if re.match(r"\s*\d", line))


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--rpk", default="docker compose exec -T redpanda rpk")
    args = p.parse_args()

    real_private = (ROOT / "keys/private.pem").read_text()
    real_public = (ROOT / "keys/public.pem").read_bytes()
    with httpx.Client(base_url=args.url, timeout=15) as client:
        r = client.post("/token", json=credentials("merchant-1"))
        r.raise_for_status()
        tokens = make_tokens(real_private, real_public, r.json()["access_token"])

        before = raw_count(args.rpk)
        results = {kind: Counter() for kind in tokens}
        kinds = list(tokens)
        for i in range(TOTAL):
            kind = kinds[i % len(kinds)]
            payment = {"txn_id": f"st1-{uuid.uuid4()}", "user_id": "st1-user", "amount": 10.0,
                       "ts": "2026-10-19T10:00:00Z"}
            status = client.post(SCORE, json=payment,
                                 headers={"Authorization": f"Bearer {tokens[kind]}"}).status_code
            results[kind][status] += 1
        time.sleep(2)   # anything that did get through has time to land in txn.raw
        after = raw_count(args.rpk)

    print("| Bad token | Sent | Answers |")
    print("|---|---:|---|")
    for kind, codes in results.items():
        print(f"| {kind} | {sum(codes.values())} | {dict(codes)} |")
    refused = sum(n for codes in results.values() for s, n in codes.items() if s in (401, 403))
    print(f"\nRefused (401 or 403): {refused:,} of {TOTAL:,}")
    print(f"txn.raw messages: {before:,} before, {after:,} after")
    other = {s for codes in results.values() for s in codes} - {401}
    if other:
        print(f"Note: answers other than 401: {sorted(other)}")
    if refused != TOTAL or after != before:
        sys.exit("ST1 FAILED")
    print("ST1 PASSED: every bad token refused, nothing reached ingest")


if __name__ == "__main__":
    main()
