"""token-issuer: gives a known client a short-lived signed token. Design: docs/access.md

POST /token with {"client_id": ..., "client_secret": ...}. The token says who the client is
(`iss`, which Kong uses to find the client) and its role, and expires after 15 minutes.
"""

import hashlib
import hmac
import json
import os
import time
from pathlib import Path

import jwt
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from observability.http import instrument

KEY_PATH = Path(os.getenv("PRIVATE_KEY_PATH", "/keys/private.pem"))
CLIENTS_PATH = Path(os.getenv("CLIENTS_PATH", "/clients/clients.issuer.json"))
TTL_SECONDS = 15 * 60   # threat model: short-lived tokens limit what a stolen one is worth

app = FastAPI(title="token-issuer")
instrument(app, "token-issuer")   # counts 401s too: a burst of them is someone guessing secrets
KEY = KEY_PATH.read_text() if KEY_PATH.exists() else ""
# client id -> {"role", "secret_sha256"}; the issuer never holds the secrets themselves
CLIENTS = json.loads(CLIENTS_PATH.read_text()) if CLIENTS_PATH.exists() else {}
# Compared against when the client id is unknown, so the answer takes as long either way
_NO_CLIENT = {"role": None, "secret_sha256": "0" * 64}


class Credentials(BaseModel):
    client_id: str = Field(min_length=1, max_length=64)
    client_secret: str = Field(min_length=1, max_length=128)


def make_token(key, client_id, role, ttl=TTL_SECONDS):
    now = int(time.time())
    claims = {"iss": client_id, "sub": client_id, "role": role, "iat": now, "exp": now + ttl}
    return jwt.encode(claims, key, algorithm="RS256")


def check(clients, client_id, secret):
    """The client's role, or None. Constant-time compare: response time doesn't hint at
    how much of a guess was right."""
    client = clients.get(client_id, _NO_CLIENT)
    given = hashlib.sha256(secret.encode()).hexdigest()
    if hmac.compare_digest(given, client["secret_sha256"]) and client["role"]:
        return client["role"]
    return None


@app.post("/token")
def token(creds: Credentials):
    role = check(CLIENTS, creds.client_id, creds.client_secret)
    if role is None:
        # The same answer for an unknown client and a wrong secret: no hint which it was
        raise HTTPException(401, "invalid client credentials")
    return {"access_token": make_token(KEY, creds.client_id, role), "token_type": "bearer",
            "expires_in": TTL_SECONDS}
