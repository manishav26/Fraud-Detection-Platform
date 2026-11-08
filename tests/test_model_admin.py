"""model-admin-svc: who may change the live model, and what gets recorded (week 4, step 6)."""

import importlib
import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from registry.signing import sign_promotion

ROOT = Path(__file__).resolve().parents[1]
# Several services have a package called `app`; load this one under its own name
_pkg = ROOT / "services/model-admin-svc/app"
_spec = importlib.util.spec_from_file_location("model_admin_app", _pkg / "__init__.py",
                                               submodule_search_locations=[str(_pkg)])
sys.modules["model_admin_app"] = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sys.modules["model_admin_app"])
service = importlib.import_module("model_admin_app.main")
rules = importlib.import_module("model_admin_app.rules")
Refused = importlib.import_module("model_admin_app.store").Refused


def key_pair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private, public


PRIVATE, PUBLIC = key_pair()
HASHES = {"model.txt": "a" * 64, "meta.json": "b" * 64}
ADMIN = {"x-consumer-username": "admin", "x-consumer-groups": "admin"}


def signed(version="4", sequence=100, action="promote", passed=True, reason="", key=PRIVATE,
           **extra):
    gate = {"passed": passed, "pr_auc_gain": 0.02}
    return sign_promotion("fraud-lgbm", version, HASHES, sequence, key, action=action,
                          label=f"fraud-lgbm-v1-{version}aaaaaaa", reason=reason, gate=gate,
                          **extra)


class FakeStore:
    """The audit log's model rows in a list, newest first, with the real rules."""

    def __init__(self):
        self.rows = []

    async def history(self, limit=50):
        return self.rows[:limit]

    async def record(self, actor, route, claims, check):
        reason = check(route, claims, self.rows)
        if reason:
            raise Refused(reason)
        self._add(actor, rules.ACTIONS[route], rules.details(claims))

    async def record_failure(self, actor, claims, error):
        self._add(actor, rules.FAILED, {"sequence": claims["sequence"], "error": error})

    def _add(self, actor, action, details):
        self.rows.insert(0, {"id": len(self.rows) + 1, "at": datetime.now(UTC), "actor": actor,
                             "action": action, "target": f"model:{details.get('version')}",
                             "details": details})


@pytest.fixture
def setup():
    store, published = FakeStore(), []

    async def publish(token):
        published.append(token)

    service.app.state.store = store
    service.app.state.public_key = PUBLIC
    service.app.state.publish = publish
    return TestClient(service.app), store, published   # no `with`: no Postgres or Redpanda


def promote(client, token, route="promote", headers=ADMIN):
    return client.post(f"/v1/model/{route}", json={"token": token}, headers=headers)


# ---------- who may ----------

@pytest.mark.parametrize("headers", [
    {},                                                          # skipped Kong
    {"x-consumer-username": "ana", "x-consumer-groups": "analyst"},
    {"x-consumer-username": "merchant-1", "x-consumer-groups": "client"},
])
def test_only_an_admin_through_kong_may_change_or_read(setup, headers):
    client, store, published = setup
    assert promote(client, signed(), headers=headers).status_code == 403
    assert client.get("/v1/model", headers=headers).status_code == 403
    assert client.get("/v1/model/history", headers=headers).status_code == 403
    assert store.rows == [] and published == []


# ---------- promote ----------

def test_genuine_promotion_is_recorded_then_published(setup):
    client, store, published = setup
    token = signed()
    r = promote(client, token)
    assert r.status_code == 200 and r.json()["by"] == "admin"
    assert published == [token]   # exactly what was signed, untouched
    row = store.rows[0]
    assert row["action"] == "model_promoted" and row["actor"] == "admin"
    assert row["details"]["version"] == "4" and row["details"]["sha256"] == HASHES
    assert row["details"]["gate"]["passed"] is True


def test_live_shows_what_was_promoted_by_whom(setup):
    client, _, _ = setup
    assert client.get("/v1/model", headers=ADMIN).json()["live"] is None
    promote(client, signed(version="4", reason="tuned v1"))
    live = client.get("/v1/model", headers=ADMIN).json()["live"]
    assert live["version"] == "4" and live["by"] == "admin" and live["reason"] == "tuned v1"


def test_forged_message_is_refused_and_nothing_recorded(setup):
    client, store, published = setup
    other, _ = key_pair()
    r = promote(client, signed(key=other))
    assert r.status_code == 400 and "not a genuine promotion" in r.json()["detail"]
    assert store.rows == [] and published == []


def test_replayed_message_is_refused(setup):
    client, _, published = setup
    token = signed(sequence=100)
    assert promote(client, token).status_code == 200
    r = promote(client, token)
    assert r.status_code == 409 and "replayed or old" in r.json()["detail"]
    assert len(published) == 1


def test_older_message_is_refused(setup):
    client, _, _ = setup
    promote(client, signed(sequence=200))
    assert promote(client, signed(version="3", sequence=150)).status_code == 409


def test_gate_said_keep_needs_a_reason(setup):
    client, store, _ = setup
    r = promote(client, signed(passed=False))
    assert r.status_code == 409 and "override needs a reason" in r.json()["detail"]
    assert promote(client, signed(passed=False, reason="demo", sequence=101)).status_code == 200
    assert store.rows[0]["details"]["reason"] == "demo"


def test_message_without_a_gate_result_is_refused(setup):
    client, _, _ = setup
    token = sign_promotion("fraud-lgbm", "4", HASHES, 100, PRIVATE, action="promote")
    assert promote(client, token).status_code == 409


def test_rollback_message_cannot_be_sent_as_a_promotion(setup):
    client, _, _ = setup
    r = promote(client, signed(action="rollback", reason="x"))
    assert r.status_code == 409 and "not 'promote'" in r.json()["detail"]


def test_failed_publish_is_recorded_and_the_token_cannot_be_reused(setup):
    client, store, _ = setup

    async def down(token):
        raise TimeoutError("Redpanda down")

    service.app.state.publish = down
    token = signed()
    assert promote(client, token).status_code == 503
    assert [r["action"] for r in store.rows] == ["model_promotion_failed", "model_promoted"]
    assert client.get("/v1/model", headers=ADMIN).json()["live"] is None   # nothing went out
    assert promote(client, token).status_code == 409   # sign again for a new sequence


def test_no_public_key_refuses_everything(setup):
    client, store, _ = setup
    service.app.state.public_key = None
    assert promote(client, signed()).status_code == 503 and store.rows == []


# ---------- rollback ----------

def test_rollback_goes_back_to_the_previous_version(setup):
    client, store, published = setup
    promote(client, signed(version="4", sequence=100))
    promote(client, signed(version="5", sequence=200))
    r = promote(client, signed(version="4", sequence=300, action="rollback", reason="v5 bad"),
                route="rollback")
    assert r.status_code == 200 and len(published) == 3
    assert store.rows[0]["action"] == "model_rolled_back"
    assert client.get("/v1/model", headers=ADMIN).json()["live"]["version"] == "4"


def test_rollback_to_any_other_version_is_refused(setup):
    client, _, _ = setup
    for version, sequence in (("3", 100), ("4", 200), ("5", 300)):
        promote(client, signed(version=version, sequence=sequence))
    r = promote(client, signed(version="3", sequence=400, action="rollback", reason="x"),
                route="rollback")
    assert r.status_code == 409 and "version 4" in r.json()["detail"]


def test_rollback_needs_a_reason_and_something_to_go_back_to(setup):
    client, _, _ = setup
    promote(client, signed(version="4", sequence=100))
    nothing = promote(client, signed(version="3", sequence=200, action="rollback", reason="x"),
                      route="rollback")
    assert "nothing to roll back to" in nothing.json()["detail"]
    promote(client, signed(version="5", sequence=300))
    no_reason = promote(client, signed(version="4", sequence=400, action="rollback"),
                        route="rollback")
    assert no_reason.status_code == 409 and "needs a reason" in no_reason.json()["detail"]


def test_failed_publishes_are_skipped_when_finding_the_previous_version():
    store = FakeStore()
    for version, sequence in (("4", 100), ("5", 200)):
        store._add("admin", "model_promoted", {"version": version, "sequence": sequence})
    store._add("admin", "model_promoted", {"version": "6", "sequence": 300})
    store._add("admin", "model_promotion_failed", {"sequence": 300})
    claims = {"action": "rollback", "model": "fraud-lgbm", "version": "4", "sequence": 400,
              "reason": "x"}
    assert rules.check("rollback", claims, store.rows) is None   # 6 never went out: 5 -> 4
