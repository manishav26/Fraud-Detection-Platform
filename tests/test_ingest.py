import asyncio
import base64
import json
import time

import pytest
from aiokafka.errors import KafkaTimeoutError
from app import main  # ingest-svc
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from pseudonym import is_pseudonym, pseudonym

GOOD = {"txn_id": "t1", "user_id": "u1", "amount": 10, "ts": "2026-10-19T10:00:00Z",
        "attrs": {"card1": 1234, "ProductCD": "W"}}


def token(age=0, claims=None):
    """What Kong passes on: the caller's token. Only its iat is read here (Kong checked the
    signature), so the signature part can be anything."""
    body = claims if claims is not None else {"iss": "merchant-1", "iat": int(time.time()) - age}
    payload = base64.urlsafe_b64encode(json.dumps(body).encode()).rstrip(b"=").decode()
    return f"Bearer eyJhbGciOiJSUzI1NiJ9.{payload}.c2ln"


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
    # no `with`: skips startup, so no real Redpanda needed. A fresh token on every request
    yield TestClient(main.app, headers={"Authorization": token()})
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
    r = TestClient(main.app, raise_server_exceptions=False,
                   headers={"Authorization": token()}).post("/v1/transactions/score", json=GOOD)
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


# ---------- replay window (threat 6, ST12) ----------

@pytest.mark.parametrize("age, status", [(0, 200), (60, 200), (320, 200), (340, 401), (600, 401),
                                         (-20, 200), (-60, 401)])
def test_a_payment_needs_a_token_issued_in_the_last_5_minutes(client, age, status):
    r = client.post("/v1/transactions/score", json=GOOD, headers={"Authorization": token(age)})
    assert r.status_code == status
    if status == 401:
        assert "too old" in r.json()["detail"]
        assert main.app.state.producer.sent == []   # nothing stored


@pytest.mark.parametrize("auth", [None, "", "Basic abc", "Bearer not-a-jwt", "Bearer a.b.c",
                                  token(claims={"iss": "merchant-1"}),        # no iat
                                  token(claims={"iat": "yesterday"})])
def test_no_readable_token_is_refused(client, auth):
    headers = {"Authorization": auth} if auth is not None else {"Authorization": ""}
    r = client.post("/v1/transactions/score", json=GOOD, headers=headers)
    assert r.status_code == 401 and main.app.state.producer.sent == []


def test_the_same_payment_twice_gets_the_same_answer(client):
    first = client.post("/v1/transactions/score", json=GOOD).json()
    again = client.post("/v1/transactions/score", json=GOOD).json()
    assert first == again   # txn_id is the idempotency key: scoring and history are upserts


# ---------- ids are id characters only (ST6) ----------

@pytest.mark.parametrize("bad_id", ["' OR '1'='1", "t1'; DROP TABLE cases; --", "t1\nERROR forged",
                                    "../../etc/passwd", "<script>", "t 1", "t1\x00", "{{7*7}}"])
@pytest.mark.parametrize("field", ["txn_id", "user_id"])
def test_ids_with_anything_but_id_characters_are_refused(client, field, bad_id):
    r = client.post("/v1/transactions/score", json={**GOOD, field: bad_id})
    assert r.status_code == 422
    [(topic, _, _)] = main.app.state.producer.sent
    assert topic == "dlq.txn"


@pytest.mark.parametrize("good_id", ["3367129", "9500|204.0|98.0", "2884|299.0|-400.0",
                                     "9500|NA|98.0", "st2-0b6f-41c2", "syn-user-7", "p1_ab12"])
def test_every_id_our_clients_send_is_accepted(client, good_id):
    r = client.post("/v1/transactions/score", json={**GOOD, "txn_id": good_id, "user_id": good_id})
    assert r.status_code == 200


# ---------- the attrs contract (features/contract.py; found by ST5) ----------

@pytest.mark.parametrize("attrs", [{"D15": "AAA"}, {"card1": "9500"}, {"card1": True},
                                   {"card4": 5}, {"not_an_attr": 1}, {"card4": "x" * 257}])
def test_attrs_that_break_the_contract_get_422_and_go_to_the_dlq(client, attrs):
    r = client.post("/v1/transactions/score", json={**GOOD, "attrs": attrs})
    assert r.status_code == 422
    assert [t for t, _, _ in main.app.state.producer.sent] == ["dlq.txn"]   # nothing in txn.raw


def test_nan_in_a_number_attr_gets_422_without_echoing_it(client):
    """Python's JSON reader takes NaN; the answer must still be a 422, not a 500 from trying to
    echo NaN back (found writing this test)."""
    body = json.dumps({**GOOD, "attrs": {"D1": 1.0}}).replace("1.0", "NaN")
    r = client.post("/v1/transactions/score", content=body,
                    headers={"content-type": "application/json"})
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "attrs", "D1"]


def test_a_422_never_echoes_what_was_sent(client):
    r = client.post("/v1/transactions/score", json={**GOOD, "txn_id": "<script>x</script>"})
    assert r.status_code == 422 and "<script>" not in r.text
    assert set(r.json()["detail"][0]) == {"loc", "msg", "type"}


def test_the_attrs_are_recorded_as_sent(client):
    client.post("/v1/transactions/score", json={**GOOD, "attrs": {"card1": 1234, "card4": "visa"}})
    [(_, _, value)] = main.app.state.producer.sent
    assert value["attrs"] == {"card1": 1234.0, "card4": "visa"}   # no nulls added for the rest


# ---------- scoring fails after the payment is recorded: 503, retry ----------

def failing_scoring(fails):
    async def broken(record):
        raise fails
    return broken


@pytest.mark.parametrize("error, kind", [
    (main.httpx.HTTPStatusError("500", request=main.httpx.Request("POST", "http://s/score"),
                                response=main.httpx.Response(500)), "error_answer"),
    (main.httpx.ReadTimeout("slow"), "timeout"),
    (main.httpx.ConnectError("refused"), "connection"),
    (ValueError("not JSON"), "bad_answer"),
])
def test_a_scoring_failure_is_a_503_that_says_retry(client, monkeypatch, error, kind):
    monkeypatch.setattr(main, "call_scoring", failing_scoring(error))
    before = sample("ingest_scoring_failures_total", kind=kind)
    r = client.post("/v1/transactions/score", json=GOOD)
    assert r.status_code == 503 and "retry with the same txn_id" in r.json()["detail"]
    assert sample("ingest_scoring_failures_total", kind=kind) == before + 1
    run_background()
    assert [t for t, _, _ in main.app.state.producer.sent] == ["txn.raw"]   # recorded, no decision


def test_an_answer_that_is_not_a_decision_is_a_503(client, monkeypatch):
    async def odd(record):
        return {"txn_id": record["txn_id"], "decision": "approve"}   # no score, no model_version
    monkeypatch.setattr(main, "call_scoring", odd)
    assert client.post("/v1/transactions/score", json=GOOD).status_code == 503


# ---------- the payment's time (features/contract.py; found by ST5 via the feature job) ----------

@pytest.mark.parametrize("ts", ["0387-08-26T14:46:05-21:11",   # what crash-looped the Spark job
                                "2026-01-01T00:00:00+14:30", "2026-01-01T00:00:00-12:01",
                                "1969-12-31T23:59:59Z", "2100-01-01T00:00:00Z",
                                "2026-01-01T00:00:00", "yesterday"])
def test_a_time_outside_the_contract_gets_422(client, ts):
    r = client.post("/v1/transactions/score", json={**GOOD, "ts": ts})
    assert r.status_code == 422
    assert [t for t, _, _ in main.app.state.producer.sent] == ["dlq.txn"]


@pytest.mark.parametrize("ts, stored", [("2017-12-02T00:00:00Z", "2017-12-02T00:00:00Z"),
                                        ("2026-11-10T09:30:00+05:30", "2026-11-10T04:00:00Z"),
                                        ("2026-01-01T00:00:00-12:00", "2026-01-01T12:00:00Z")])
def test_the_time_is_stored_in_utc(client, ts, stored):
    client.post("/v1/transactions/score", json={**GOOD, "ts": ts})
    [(_, _, value)] = main.app.state.producer.sent
    assert value["ts"] == stored
