import asyncio
import json

import pytest
from aiokafka.errors import KafkaTimeoutError
from app import main  # ingest-svc
from fastapi.testclient import TestClient

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


def test_good_payment_is_saved_then_decided(client):
    r = client.post("/v1/transactions/score", json=GOOD)
    assert r.status_code == 200
    assert r.json()["decision"] == "approve" and r.json()["cold_start"] is True
    [(topic, key, value)] = main.app.state.producer.sent
    assert topic == "txn.raw" and key == b"u1"
    assert value["txn_id"] == "t1" and value["attrs"]["card1"] == 1234


def test_decision_is_published_to_txn_scored_after_answering(client):
    client.post("/v1/transactions/score", json=GOOD)
    assert [t for t, _, _ in main.app.state.producer.sent] == ["txn.raw"]   # not yet
    run_background()
    topic, key, value = main.app.state.producer.sent[1]
    assert topic == "txn.scored" and key == b"u1"
    assert value["txn_id"] == "t1" and value["amount"] == 10.0          # the payment
    assert value["decision"] == "approve" and value["score"] == 0.12    # and its decision
    assert "reasons" in value and "degraded" in value and value["scored_at"]


def test_failed_scored_publish_still_answers_and_is_counted(client, monkeypatch):
    monkeypatch.setitem(main.counters, "scored_publish_failures", 0)
    main.app.state.producer = FakeProducer(fail_topics={"txn.scored"})
    r = client.post("/v1/transactions/score", json=GOOD)
    assert r.status_code == 200 and r.json()["decision"] == "approve"
    run_background()
    assert main.counters["scored_publish_failures"] == 1


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
    assert topic == "dlq.txn" and key is None
    assert json.loads(value["body"]) == bad and value["errors"]


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
