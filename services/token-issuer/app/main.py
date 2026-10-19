import os
import time
from pathlib import Path

import jwt
from fastapi import FastAPI

app = FastAPI(title="token-issuer (dev only)")
KEY_PATH = os.getenv("PRIVATE_KEY_PATH", "/keys/private.pem")
KEY = Path(KEY_PATH).read_text() if Path(KEY_PATH).exists() else ""


def make_token(key: str, ttl: int = 3600) -> str:
    claims = {"iss": "fraud-platform-issuer", "scope": "score:write",
              "iat": int(time.time()), "exp": int(time.time()) + ttl}
    return jwt.encode(claims, key, algorithm="RS256")


@app.post("/token")
def token():
    # Dev-only: no client auth. Replace with client credentials before any real use.
    return {"access_token": make_token(KEY), "token_type": "bearer"}
