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

    def __init__(self, fail=False, hang=False):
        self.sent, self.fail, self.hang = [], fail, hang

    async def send_and_wait(self, topic, value=None, key=None):
        if self.fail:
            raise KafkaTimeoutError()
        if self.hang:   # what aiokafka really does while the broker is down
            await asyncio.sleep(3600)
        self.sent.append((topic, key, value))


@pytest.fixture
def client(monkeypatch):
    async def fake_scoring(record):
        return {"score": 0.12, "model_version": "stub-0"}

    monkeypatch.setattr(main, "call_scoring", fake_scoring)
    main.app.state.producer = FakeProducer()
    return TestClient(main.app)   # no `with`: skips startup, so no real Redpanda needed


def test_good_payment_is_saved_then_decided(client):
    r = client.post("/v1/transactions/score", json=GOOD)
    assert r.status_code == 200
    assert r.json()["decision"] == "approve"
    [(topic, key, value)] = main.app.state.producer.sent
    assert topic == "txn.raw" and key == b"u1"
    assert value["txn_id"] == "t1" and value["attrs"]["card1"] == 1234


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
