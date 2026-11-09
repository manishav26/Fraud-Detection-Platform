import asyncio
import json

import pytest
from aiokafka.errors import KafkaTimeoutError
from app import main  # ingest-svc
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from pseudonym import is_pseudonym, pseudonym

GOOD = {"txn_id": "t1", "user_id": "u1", "amount": 10, "ts": "2026-10-19T10:00:00Z",
        "attrs": {"card1": 1234, "ProductCD": "W"}}


class FakeProducer:
    """Stands in for Redpanda: remembers what was sent, or fails like a broker that is down."""

    def __init__(self, fail=False, hang=False, fail_topics=()):
        self.sent, self.fail, self.hang, self.fail_topics = [], fail, hang, fail_topics

    async def send_and_wait(self, topic, value=None, key=None):
        if self.fail or topic in self.fail_topics:
            raise KafkaTimeoutError()
        if self.hang:   # what aiokafka really does while the broker is down
            await asyncio.sleep(3600)
        self.sent.append((topic, key, value))


@pytest.fixture
def client(monkeypatch):
    async def fake_scoring(record):
        return {"txn_id": record["txn_id"], "score": 0.12, "decision": "approve",
                "model_version": "stub-0", "reasons": [], "cold_start": True}

    monkeypatch.setattr(main, "call_scoring", fake_scoring)
    # The test client stops its event loop after each request, which would cancel background
    # work. So collect it here, and tests run it with run_background().
    monkeypatch.setattr(main, "in_background", background.append)
    main.app.state.producer = FakeProducer()
    yield TestClient(main.app)   # no `with`: skips startup, so no real Redpanda needed
    discard_background()


background = []


def discard_background():
    """Close work a test didn't run, so Python doesn't warn that it was never awaited."""
    for coroutine in background:
        coroutine.close()
    background.clear()


def run_background():
    for coroutine in background:
        asyncio.run(coroutine)
    background.clear()


TESTER = {"x-consumer-groups": "tester"}   # what Kong adds for the load-test client


def test_good_payment_is_saved_then_decided(client):
    r = client.post("/v1/transactions/score", json=GOOD, headers=TESTER)
    assert r.status_code == 200
    assert r.json()["decision"] == "approve" and r.json()["cold_start"] is True
    [(topic, key, value)] = main.app.state.producer.sent
    assert topic == "txn.raw" and key == pseudonym("u1").encode()
    assert value["user_id"] == pseudonym("u1") and value["user_id"] != "u1"
    assert value["txn_id"] == "t1" and value["attrs"]["card1"] == 1234


def test_decision_is_published_to_txn_scored_after_answering(client):
    client.post("/v1/transactions/score", json=GOOD)
    assert [t for t, _, _ in main.app.state.producer.sent] == ["txn.raw"]   # not yet
    run_background()
    topic, key, value = main.app.state.producer.sent[1]
    assert topic == "txn.scored" and key == pseudonym("u1").encode()
    assert value["user_id"] == pseudonym("u1")
    assert value["txn_id"] == "t1" and value["amount"] == 10.0          # the payment
    assert value["decision"] == "approve" and value["score"] == 0.12    # and its decision
    assert "reasons" in value and "degraded" in value and value["scored_at"]


def failures():
    return sample("ingest_scored_publish_failures_total")


def test_failed_scored_publish_still_answers_and_is_counted(client):
    before = failures()
    main.app.state.producer = FakeProducer(fail_topics={"txn.scored"})
    r = client.post("/v1/transactions/score", json=GOOD)
    assert r.status_code == 200 and r.json()["decision"] == "approve"
    run_background()
    assert failures() == before + 1


def sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0


def test_metrics_count_requests_and_steps(client):
    request = {"service": "ingest-svc", "route": "/v1/transactions/score", "method": "POST",
               "status": "200"}
    before = sample("http_requests_total", **request)
    steps = sample("ingest_step_seconds_count", step="publish_raw")
    client.post("/v1/transactions/score", json=GOOD)
    assert sample("http_requests_total", **request) == before + 1
    assert sample("ingest_step_seconds_count", step="publish_raw") == steps + 1

    page = client.get("/metrics")
    assert page.status_code == 200 and "http_request_duration_seconds_bucket" in page.text
    assert 'route="/metrics"' not in page.text   # the page doesn't count itself


@pytest.mark.parametrize("bad", [
    {**GOOD, "amount": -5},                       # amount must be > 0
    {**GOOD, "ts": "2026-10-19T10:00:00"},        # no timezone
    {**GOOD, "txn_id": ""},                       # empty id
    {k: v for k, v in GOOD.items() if k != "user_id"},
])
def test_bad_payment_goes_to_dlq_and_gets_422(client, bad):
    r = client.post("/v1/transactions/score", json=bad)
    assert r.status_code == 422
    [(topic, key, value)] = main.app.state.producer.sent
    assert topic == "dlq.txn" and key is None and value["errors"]
    # the copy is kept, with only its user id pseudonymized (docs/privacy.md)
    kept = json.loads(value["body"])
    assert {k: v for k, v in kept.items() if k != "user_id"} == \
        {k: v for k, v in bad.items() if k != "user_id"}
    assert kept.get("user_id") == (pseudonym(bad["user_id"]) if "user_id" in bad else None)


def test_not_json_goes_to_dlq(client):
    r = client.post("/v1/transactions/score", content=b"not json",
                    headers={"content-type": "application/json"})
    assert r.status_code == 422
    assert main.app.state.producer.sent[0][2]["body"] == "not json"


@pytest.mark.parametrize("broken", [FakeProducer(fail=True), FakeProducer(hang=True)])
def test_redpanda_down_gives_503_and_no_score(client, monkeypatch, broken):
    async def must_not_score(record):
        raise AssertionError("scored a payment that was not recorded")

    monkeypatch.setattr(main, "call_scoring", must_not_score)
    monkeypatch.setattr(main, "SEND_TIMEOUT", 0.1)
    main.app.state.producer = broken
    r = client.post("/v1/transactions/score", json=GOOD)
    assert r.status_code == 503
    assert background == []   # nothing to publish for a payment that wasn't decided


# ---------- a scoring pod going away (week 4, step 2; docs/results/scaling.md) ----------

SCORED = {"txn_id": "t1", "score": 0.12, "decision": "approve", "model_version": "stub-0",
          "reasons": [], "cold_start": False}


def scoring_that(*outcomes):
    """A fake scoring-svc: each call takes the next outcome, an exception or a 200 answer."""
    import httpx

    calls = []

    def handler(request):
        outcome = outcomes[len(calls)]
        calls.append(request)
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(200, json=SCORED)

    return httpx.AsyncClient(base_url="http://scoring", transport=httpx.MockTransport(handler)), calls


def test_broken_scoring_connection_is_tried_once_more():
    import httpx

    main.app.state.http, calls = scoring_that(httpx.RemoteProtocolError("closed"), "ok")
    before = REGISTRY.get_sample_value("ingest_scoring_retries_total") or 0
    assert asyncio.run(main.call_scoring({"txn_id": "t1"}))["decision"] == "approve"
    assert len(calls) == 2
    assert REGISTRY.get_sample_value("ingest_scoring_retries_total") == before + 1


def test_scoring_is_not_retried_after_a_timeout():
    import httpx

    main.app.state.http, calls = scoring_that(httpx.ReadTimeout("slow"), "ok")
    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(main.call_scoring({"txn_id": "t1"}))
    assert len(calls) == 1   # retrying would only double a slow wait


def test_a_crash_is_counted_as_a_500(monkeypatch):
    async def broken(record):
        raise RuntimeError("scoring connection broke twice")

    monkeypatch.setattr(main, "call_scoring", broken)
    main.app.state.producer = FakeProducer()
    labels = {"service": "ingest-svc", "route": "/v1/transactions/score", "method": "POST",
              "status": "500"}
    before = REGISTRY.get_sample_value("http_requests_total", labels) or 0
    r = TestClient(main.app, raise_server_exceptions=False).post("/v1/transactions/score",
                                                                 json=GOOD)
    assert r.status_code == 500
    assert REGISTRY.get_sample_value("http_requests_total", labels) == before + 1


# ---------- E6 defense D1: clients see only the decision ----------

@pytest.mark.parametrize("groups", ["client", None, "", "analyst", "admin", "testers"])
def test_clients_and_unknown_roles_get_only_the_decision(client, groups):
    headers = {} if groups is None else {"x-consumer-groups": groups}
    r = client.post("/v1/transactions/score", json=GOOD, headers=headers)
    assert r.status_code == 200 and r.json() == {"txn_id": "t1", "decision": "approve"}


@pytest.mark.parametrize("groups", ["tester", "client, tester"])
def test_tester_gets_the_full_answer(client, groups):
    body = client.post("/v1/transactions/score", json=GOOD,
                       headers={"x-consumer-groups": groups}).json()
    assert body["score"] == 0.12 and body["model_version"] == "stub-0"
    assert body["probe"] is False and body["cold_start"] is True


def test_txn_scored_keeps_the_full_answer_for_a_client(client):
    client.post("/v1/transactions/score", json=GOOD, headers={"x-consumer-groups": "client"})
    run_background()
    [value] = [v for t, _, v in main.app.state.producer.sent if t == "txn.scored"]
    assert value["score"] == 0.12 and value["model_version"] == "stub-0"
    assert value["probe"] is False


# ---------- pseudonyms (threat 8, docs/privacy.md) ----------

def test_scoring_gets_only_the_pseudonym(client, monkeypatch):
    seen = []

    async def fake_scoring(record):
        seen.append(record["user_id"])
        return {"txn_id": record["txn_id"], "score": 0.1, "decision": "approve",
                "model_version": "stub-0"}
    monkeypatch.setattr(main, "call_scoring", fake_scoring)
    client.post("/v1/transactions/score", json={**GOOD, "user_id": "9500|204.0|98.0"})
    assert seen == [pseudonym("9500|204.0|98.0")] and is_pseudonym(seen[0])


def test_a_bad_body_with_a_number_or_no_user_id_is_kept_as_sent(client):
    body = {**GOOD, "user_id": 12345}   # not text: refused, and nothing to pseudonymize
    client.post("/v1/transactions/score", json=body)
    assert json.loads(main.app.state.producer.sent[0][2]["body"]) == body
