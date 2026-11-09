"""The token issuer: only known clients with the right secret get a token, for their own role."""

import hashlib
import importlib.util
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
# Several services have a package called `app`; load the issuer's by path
_spec = importlib.util.spec_from_file_location("token_issuer",
                                               ROOT / "services/token-issuer/app/main.py")
issuer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(issuer)

_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE = _key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()
PUBLIC = _key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo)


def hashed(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(issuer, "KEY", PRIVATE)
    monkeypatch.setattr(issuer, "CLIENTS", {
        "ana": {"role": "analyst", "secret_sha256": hashed("ana-secret")},
        "merchant-1": {"role": "client", "secret_sha256": hashed("m1-secret")}})
    return TestClient(issuer.app)


def test_known_client_gets_a_15_minute_token_for_its_own_role(client):
    r = client.post("/token", json={"client_id": "ana", "client_secret": "ana-secret"})
    assert r.status_code == 200 and r.json()["expires_in"] == 900
    claims = jwt.decode(r.json()["access_token"], PUBLIC, algorithms=["RS256"])
    assert claims["iss"] == "ana" and claims["sub"] == "ana" and claims["role"] == "analyst"
    assert 890 <= claims["exp"] - time.time() <= 900


@pytest.mark.parametrize("creds", [
    {"client_id": "ana", "client_secret": "wrong"},
    {"client_id": "ana", "client_secret": "m1-secret"},      # another client's secret
    {"client_id": "nobody", "client_secret": "ana-secret"},
])
def test_wrong_or_unknown_client_gets_the_same_401(client, creds):
    r = client.post("/token", json=creds)
    assert r.status_code == 401 and r.json()["detail"] == "invalid client credentials"


@pytest.mark.parametrize("body", [{}, {"client_id": "ana"}, {"client_id": "", "client_secret": "x"}])
def test_missing_credentials_are_rejected(client, body):
    assert client.post("/token", json=body).status_code == 422


def test_issuer_holds_no_plain_secrets(client):
    assert all(set(c) == {"role", "secret_sha256"} for c in issuer.CLIENTS.values())


def test_roster_has_every_role_and_the_two_merchants_st7_needs():
    import json
    roster = json.loads((ROOT / "deploy/clients.json").read_text())
    roles = {v for k, v in roster.items() if not k.startswith("_")}
    assert roles == {"client", "analyst", "admin", "case-system"}
    assert roster["merchant-1"] == roster["merchant-2"] == roster["load-test"] == "client"
