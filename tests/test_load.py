import asyncio
import csv
import math

import httpx
import pandas as pd
from app.main import Transaction  # ingest-svc: the contract the gateway enforces

from features.proxy_id import add_proxy_id
from features.request_time import hour_of_day
from load import replay, sender, synthetic


def stream_rows():
    attrs = replay.attr_columns()
    df = pd.DataFrame({c: [math.nan, math.nan] for c in attrs})
    df["TransactionID"] = [3000001, 3000002]
    df["TransactionDT"] = [9_504_000 + 5 * 3600 + 10, 9_504_000 + 5 * 3600 + 70]
    df["TransactionAmt"] = [49.5, 12.0]
    df["isFraud"] = [0, 1]
    df["card1"], df["addr1"], df["D1"] = [9500, 9500], [204.0, 204.0], [12.0, 12.0]
    df["ProductCD"], df["DeviceType"] = ["W", "W"], [None, "mobile"]
    return add_proxy_id(df)


def test_attrs_are_the_51_contract_columns():
    attrs = replay.attr_columns()
    assert len(attrs) == len(set(attrs)) == 51


def test_replay_request_matches_contract_and_training():
    _, req, label = next(replay.schedule(stream_rows(), speed=0))
    Transaction.model_validate(req)                     # ingest would accept it
    assert req["txn_id"] == "3000001" and req["user_id"] == stream_rows()["uid"][0]
    assert req["ts"].endswith("05:00:10Z")              # midnight UTC + TransactionDT
    assert int(req["ts"][11:13]) == hour_of_day(9_504_000 + 5 * 3600 + 10)
    assert req["attrs"]["card1"] == 9500 and req["attrs"]["M1"] is None
    assert len(req["attrs"]) == 51 and label == 0


def test_replay_speed_compresses_time():
    offsets = [o for o, _, _ in replay.schedule(stream_rows(), speed=60)]
    assert offsets == [0, 1.0]   # 60 s of data in 1 s


def test_synthetic_requests_match_contract_and_repeat():
    a = list(synthetic.schedule(rps=10, seconds=2, users=50, seed=3))
    b = list(synthetic.schedule(rps=10, seconds=2, users=50, seed=3))
    assert a == b and len(a) == 20 and a[-1][0] == 1.9
    for _, req, _ in a:
        Transaction.model_validate(req)


def test_sender_retries_503_and_refreshes_expired_token(tmp_path, monkeypatch):
    monkeypatch.setattr(sender, "BACKOFF", 0)
    calls = {"token": 0, "score": 0}

    def gateway(request):                      # fake Kong + services
        if request.url.path == "/token":
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": f"tok{calls['token']}"})
        calls["score"] += 1
        if request.headers["Authorization"] == "Bearer tok1":
            return httpx.Response(401, json={"message": "expired"})
        if calls["score"] == 2:
            return httpx.Response(503, json={"detail": "retry"})
        return httpx.Response(200, json={"txn_id": "x", "score": 0.1, "decision": "approve",
                                         "model_version": "stub-0", "reasons": [],
                                         "cold_start": True})

    sched = list(synthetic.schedule(rps=100, seconds=0.01, users=1, seed=1))
    stats, _ = asyncio.run(sender.run(sched, "http://test", keep_rows=True,
                                      transport=httpx.MockTransport(gateway)))
    assert stats.status == {200: 1} and stats.retries == 1 and calls["token"] == 2
    assert stats.cold_starts == 1 and "Cold starts: 1" in stats.summary(1.0)
    out = tmp_path / "results.csv"
    sender.write_csv(stats.rows, out)
    rows = list(csv.DictReader(out.open()))
    assert rows[0]["decision"] == "approve"
