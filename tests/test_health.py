"""/healthz and /readyz for Kubernetes' probes (observability/http.py; week 4, step 2)."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from observability.http import instrument


def app_with(ready=None, name="health-test"):
    app = FastAPI()
    instrument(app, name, ready=ready)

    @app.get("/work")
    def work():
        return {"ok": True}

    return TestClient(app)


def test_healthz_always_answers():
    assert app_with(ready=lambda: (False, "starting")).get("/healthz").json() == {"alive": True}


def test_readyz_follows_the_ready_check():
    state = {"ok": False}
    client = app_with(ready=lambda: (state["ok"], "model not loaded yet"))
    r = client.get("/readyz")
    assert r.status_code == 503 and r.json()["reason"] == "model not loaded yet"
    state["ok"] = True
    assert client.get("/readyz").status_code == 200


def test_ready_by_default():
    assert app_with().get("/readyz").status_code == 200


def test_probes_are_not_counted_as_traffic():
    client = app_with(name="health-count-test")
    for path in ("/healthz", "/readyz", "/healthz", "/work"):
        client.get(path)

    def count(route):
        return REGISTRY.get_sample_value("http_requests_total", {
            "service": "health-count-test", "route": route, "method": "GET", "status": "200"})

    assert count("/work") == 1
    assert count("/healthz") is None and count("/readyz") is None



def test_every_service_answers_422_without_echoing_the_input():
    """FastAPI's own 422 echoes each bad value, and fails as a 500 on NaN (found in week 5)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pydantic import BaseModel

    from observability.http import instrument

    class Body(BaseModel):
        note: str
        n: float

    app = FastAPI()
    instrument(app, "test-svc")

    @app.post("/x")
    def x(body: Body):
        return {}

    client = TestClient(app)
    r = client.post("/x", content='{"note": 5, "n": NaN}', headers={"content-type": "application/json"})
    assert r.status_code == 422 and "NaN" not in r.text
    assert all(set(p) == {"loc", "msg", "type"} for p in r.json()["detail"])
    r = client.post("/x", json={"note": "<script>", "n": "x"})
    assert r.status_code == 422 and "<script>" not in r.text
