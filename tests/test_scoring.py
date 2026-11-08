import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from store.guarded import GuardedStore

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

    def past(self, user_id, ts, timeout=None):
        if self.broken:
            raise RuntimeError("Cassandra down")
        return self.past_rows

    def save(self, txn, timeout=None):
        if self.broken:
            raise RuntimeError("Cassandra down")
        self.saved.append(txn["txn_id"])


@pytest.fixture
def client():
    use_model(FakeModel())
    return TestClient(scoring.app)   # no `with`: no real model or Cassandra needed


def use_model(model):
    scoring.app.state.live = SimpleNamespace(current=(model, "fake-v1"))
    return model


def current_model():
    return scoring.app.state.live.current[0]


def use_store(store):
    scoring.app.state.store = GuardedStore(store)
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


def test_pinned_model_gets_the_same_label_as_if_promoted(tmp_path, monkeypatch):
    model_folder(tmp_path)
    (tmp_path / "meta.json").write_text('{"info": {"name": "fraud-lgbm-v1"}}')
    pins = {f: scoring.sha256(tmp_path / f) for f in scoring.FILES}
    for f, env in scoring.PIN_ENV.items():
        monkeypatch.setenv(env, pins[f])
    monkeypatch.setattr(scoring, "MODEL_DIR", tmp_path)
    monkeypatch.setattr(scoring.FraudModel, "load", staticmethod(lambda folder: "model"))
    _, version = scoring.load_model()
    assert version == f"fraud-lgbm-v1-{pins['model.txt'][:8]}"
    assert version == scoring.model_label(tmp_path, pins["model.txt"])   # the promotion path's


def test_deploy_pins_match_the_trained_model():
    folder = ROOT / "models/fraud-lgbm-v1"
    if not (folder / "model.txt").exists():
        pytest.skip("model not trained on this machine")
    env = dict(line.split("=", 1) for line in (ROOT / "deploy/scoring.env").read_text().splitlines()
               if line and not line.startswith("#"))
    scoring.verify_pins(folder, {f: env[v] for f, v in scoring.PIN_ENV.items()})


# ---------- health (week 4, step 2) ----------

def test_not_ready_until_model_and_cassandra_are_there():
    state = scoring.app.state._state
    saved = dict(state)
    state.clear()   # as before startup has run
    try:
        client = TestClient(scoring.app)
        r = client.get("/readyz")
        assert r.status_code == 503 and "model" in r.json()["reason"]
        assert client.get("/healthz").status_code == 200   # alive, just not ready
        use_model(FakeModel())
        assert "Cassandra" in client.get("/readyz").json()["reason"]
        use_store(FakeStore())
        assert client.get("/readyz").status_code == 200
    finally:
        state.clear()
        state.update(saved)


def test_cassandra_down_later_keeps_scoring_ready():
    # Degraded answers beat errors: a broken store must not take scoring out of rotation
    use_model(FakeModel())
    use_store(FakeStore(broken=True))
    assert TestClient(scoring.app).get("/readyz").status_code == 200


# ---------- scoring a payment ----------

def test_known_user_gets_history_and_is_saved(client):
    past = [{"t": 1_000_000.0, "amount": 20.0, "P_emaildomain": "gmail.com",
             "DeviceType": "mobile", "ProductCD": "W"}]
    store = use_store(FakeStore(past))
    body = client.post("/score", json=req(KNOWN)).json()
    assert body["decision"] == "decline" and body["model_version"] == "fake-v1"
    assert body["cold_start"] is False
    assert current_model().seen_history["n_prev_txns"] == 1
    assert store.saved == ["t9"]


def test_known_user_first_payment_is_cold_start_with_zero_counts(client):
    use_store(FakeStore())
    body = client.post("/score", json=req(KNOWN)).json()
    assert body["cold_start"] is True
    assert current_model().seen_history["n_prev_txns"] == 0   # same as training


def test_unknown_user_gets_no_history_and_is_not_saved(client):
    store = use_store(FakeStore())
    body = client.post("/score", json=req(UNKNOWN)).json()
    assert body["cold_start"] is True
    assert current_model().seen_history is None
    assert store.saved == []


@pytest.mark.parametrize("prob, decision", [(0.5, "review"), (0.8, "decline")])
def test_flagged_payment_gets_reasons(client, prob, decision):
    use_store(FakeStore())
    use_model(FakeModel(prob))
    body = client.post("/score", json=req(KNOWN)).json()
    assert body["decision"] == decision and body["reasons"] == ["C1=72 (+2.32)"]


def test_approved_payment_skips_explaining(client):
    use_store(FakeStore())
    model = use_model(FakeModel(prob=0.1))
    body = client.post("/score", json=req(KNOWN)).json()
    assert body["decision"] == "approve" and body["reasons"] == []
    assert model.explained == 0


def test_explain_failure_keeps_the_decision(client):
    store = use_store(FakeStore())
    use_model(FakeModel(explain_error=True))
    r = client.post("/score", json=req(KNOWN))
    assert r.status_code == 200
    assert r.json()["decision"] == "decline" and r.json()["reasons"] == []
    assert store.saved == ["t9"]


def test_cassandra_down_still_scores(client):
    use_store(FakeStore(broken=True))
    r = client.post("/score", json=req(KNOWN))
    assert r.status_code == 200 and r.json()["degraded"] is True
    assert r.json()["cold_start"] is False    # we don't know that the user is new
    assert current_model().seen_history is None


def sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0


def test_metrics_record_each_step_and_the_decision(client):
    use_store(FakeStore())
    steps = {s: sample("scoring_step_seconds_count", step=s)
             for s in ("history_read", "predict", "reasons", "history_write")}
    declines = sample("scoring_decisions_total", decision="decline", cold_start="true",
                      degraded="false")
    client.post("/score", json=req(KNOWN))   # FakeModel declines
    for step, before in steps.items():
        assert sample("scoring_step_seconds_count", step=step) == before + 1, step
    assert sample("scoring_decisions_total", decision="decline", cold_start="true",
                  degraded="false") == declines + 1
    page = client.get("/metrics").text
    assert 'scoring_model_info{version="fake-v1"} 1.0' in page


def test_unknown_user_is_not_counted_as_a_fast_read(client):
    use_store(FakeStore())
    reads = sample("scoring_step_seconds_count", step="history_read")
    client.post("/score", json=req(UNKNOWN))
    assert sample("scoring_step_seconds_count", step="history_read") == reads


def test_normal_payment_is_not_degraded(client):
    use_store(FakeStore())
    assert client.post("/score", json=req(KNOWN)).json()["degraded"] is False
    assert client.post("/score", json=req(UNKNOWN)).json()["degraded"] is False
