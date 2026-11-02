import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]

# Both services have a package called `app`; load scoring's by path so it doesn't clash.
_spec = importlib.util.spec_from_file_location("scoring_main",
                                               ROOT / "services/scoring-svc/app/main.py")
scoring = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scoring)

KNOWN = "9500|204.0|98.0"
UNKNOWN = "9500|NA|98.0"


def req(user, ts="2017-12-10T10:00:00Z", amount=50.0):
    return {"txn_id": "t9", "user_id": user, "amount": amount, "ts": ts,
            "attrs": {"P_emaildomain": "gmail.com", "DeviceType": "mobile", "ProductCD": "W"}}


class FakeModel:
    def __init__(self, prob=0.8, explain_error=False):
        self.meta = {"thresholds": {"review_threshold": 0.39, "decline_threshold": 0.70}}
        self.seen_history = "not called"
        self.prob, self.explain_error, self.explained = prob, explain_error, 0

    def predict_one(self, request, history=None):
        self.seen_history = history
        return self.prob

    def explain_one(self, request, history=None):
        self.explained += 1
        if self.explain_error:
            raise RuntimeError("explain failed")
        return ["C1=72 (+2.32)"]

    decide = scoring.FraudModel.decide


class FakeStore:
    def __init__(self, past=(), broken=False):
        self.past_rows, self.broken, self.saved = list(past), broken, []

    def past(self, user_id, ts):
        if self.broken:
            raise RuntimeError("Cassandra down")
        return self.past_rows

    def save(self, txn):
        self.saved.append(txn["txn_id"])


@pytest.fixture
def client():
    scoring.app.state.model, scoring.app.state.version = FakeModel(), "fake-v1"
    return TestClient(scoring.app)   # no `with`: no real model or Cassandra needed


def use_store(store):
    scoring.app.state.store = store
    return store


# ---------- pinned model fingerprints (threat 11, ST9) ----------

def model_folder(tmp_path):
    for name, text in [("model.txt", "tree data"), ("meta.json", "{}")]:
        (tmp_path / name).write_text(text)
    pins = {f: scoring.sha256(tmp_path / f) for f in scoring.FILES}
    (tmp_path / "manifest.json").write_text(json.dumps(pins))
    return pins


def test_pins_accept_the_right_files(tmp_path):
    scoring.verify_pins(tmp_path, model_folder(tmp_path))


def test_pins_catch_a_swap_even_with_a_rewritten_manifest(tmp_path):
    pins = model_folder(tmp_path)
    (tmp_path / "model.txt").write_text("attacker's model")
    forged = {f: scoring.sha256(tmp_path / f) for f in scoring.FILES}
    (tmp_path / "manifest.json").write_text(json.dumps(forged))   # manifest check would pass
    with pytest.raises(RuntimeError, match="model.txt does not match"):
        scoring.verify_pins(tmp_path, pins)


def test_missing_pin_refuses_to_load(tmp_path):
    pins = model_folder(tmp_path)
    pins.pop("meta.json")
    with pytest.raises(RuntimeError, match="MODEL_SHA256_META_JSON"):
        scoring.verify_pins(tmp_path, pins)


def test_no_model_folder_means_stub(tmp_path, monkeypatch):
    monkeypatch.setattr(scoring, "MODEL_DIR", tmp_path / "missing")
    monkeypatch.setattr(scoring, "DECISION_CONFIG", ROOT / "config/decision.yaml")
    model, version = scoring.load_model()
    assert version == "stub-0" and model.decide(model.predict_one({})) == "approve"
    assert model.explain_one({}) == []


def test_deploy_pins_match_the_trained_model():
    folder = ROOT / "models/fraud-lgbm-v1"
    if not (folder / "model.txt").exists():
        pytest.skip("model not trained on this machine")
    env = dict(line.split("=", 1) for line in (ROOT / "deploy/scoring.env").read_text().splitlines()
               if line and not line.startswith("#"))
    scoring.verify_pins(folder, {f: env[v] for f, v in scoring.PIN_ENV.items()})


# ---------- scoring a payment ----------

def test_known_user_gets_history_and_is_saved(client):
    past = [{"t": 1_000_000.0, "amount": 20.0, "P_emaildomain": "gmail.com",
             "DeviceType": "mobile", "ProductCD": "W"}]
    store = use_store(FakeStore(past))
    body = client.post("/score", json=req(KNOWN)).json()
    assert body["decision"] == "decline" and body["model_version"] == "fake-v1"
    assert body["cold_start"] is False
    assert scoring.app.state.model.seen_history["n_prev_txns"] == 1
    assert store.saved == ["t9"]


def test_known_user_first_payment_is_cold_start_with_zero_counts(client):
    use_store(FakeStore())
    body = client.post("/score", json=req(KNOWN)).json()
    assert body["cold_start"] is True
    assert scoring.app.state.model.seen_history["n_prev_txns"] == 0   # same as training


def test_unknown_user_gets_no_history_and_is_not_saved(client):
    store = use_store(FakeStore())
    body = client.post("/score", json=req(UNKNOWN)).json()
    assert body["cold_start"] is True
    assert scoring.app.state.model.seen_history is None
    assert store.saved == []


@pytest.mark.parametrize("prob, decision", [(0.5, "review"), (0.8, "decline")])
def test_flagged_payment_gets_reasons(client, prob, decision):
    use_store(FakeStore())
    scoring.app.state.model = FakeModel(prob)
    body = client.post("/score", json=req(KNOWN)).json()
    assert body["decision"] == decision and body["reasons"] == ["C1=72 (+2.32)"]


def test_approved_payment_skips_explaining(client):
    use_store(FakeStore())
    scoring.app.state.model = model = FakeModel(prob=0.1)
    body = client.post("/score", json=req(KNOWN)).json()
    assert body["decision"] == "approve" and body["reasons"] == []
    assert model.explained == 0


def test_explain_failure_keeps_the_decision(client):
    store = use_store(FakeStore())
    scoring.app.state.model = FakeModel(explain_error=True)
    r = client.post("/score", json=req(KNOWN))
    assert r.status_code == 200
    assert r.json()["decision"] == "decline" and r.json()["reasons"] == []
    assert store.saved == ["t9"]


def test_cassandra_down_still_scores(client):
    use_store(FakeStore(broken=True))
    r = client.post("/score", json=req(KNOWN))
    assert r.status_code == 200 and r.json()["cold_start"] is True
