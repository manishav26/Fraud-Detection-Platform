import asyncio
import csv
import json
import math

import httpx
import pandas as pd
from app.main import Transaction  # ingest-svc: the contract the gateway enforces

from features.proxy_id import add_proxy_id
from features.request_time import hour_of_day
from load import replay, sender, synthetic

CREDS = {"client_id": "load-test", "client_secret": "s3cret"}


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


def test_replay_fixed_rate_ignores_data_times():
    offsets = [o for o, _, _ in replay.schedule(stream_rows(), speed=60, rps=4)]
    assert offsets == [0, 0.25]   # evenly spaced, though the rows are 60 s apart


def test_replay_fixed_rate_stays_even_across_chunks():
    df = pd.concat([stream_rows()] * 3, ignore_index=True)
    offsets = [o for o, _, _ in replay.schedule(df, speed=0, chunk=4, rps=2)]
    assert offsets == [i / 2 for i in range(6)]


def test_load_rows_offset_skips_the_first_payments(tmp_path):
    rows = pd.concat([stream_rows()] * 3, ignore_index=True)
    rows["TransactionID"] = range(1, 7)
    rows["TransactionDT"] = [100 * i for i in range(6)]
    path = tmp_path / "stream.parquet"
    rows.drop(columns=["uid", "uid_known"]).to_parquet(path)
    assert replay.load_rows(path, limit=2, offset=3)["TransactionID"].tolist() == [4, 5]
    assert replay.load_rows(path, offset=4)["TransactionID"].tolist() == [5, 6]


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
            assert json.loads(request.content) == CREDS   # logs in as the client it was given
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": f"tok{calls['token']}"})
        calls["score"] += 1
        if request.headers["Authorization"] == "Bearer tok1":
            return httpx.Response(401, json={"message": "expired"})
        if calls["score"] == 2:
            return httpx.Response(503, json={"detail": "retry"})
        return httpx.Response(200, json={"txn_id": "x", "score": 0.1, "decision": "approve",
                                         "model_version": "stub-0", "reasons": [],
                                         "cold_start": True, "degraded": True})

    sched = list(synthetic.schedule(rps=100, seconds=0.01, users=1, seed=1))
    stats, _ = asyncio.run(sender.run(sched, "http://test", keep_rows=True,
                                      transport=httpx.MockTransport(gateway), creds=CREDS))
    assert stats.status == {200: 1} and stats.retries == 1 and calls["token"] == 2
    assert stats.cold_starts == 1 and "Cold starts: 1" in stats.summary(1.0)
    assert stats.degraded == 1 and "without history): 1" in stats.summary(1.0)
    out = tmp_path / "results.csv"
    sender.write_csv(stats.rows, out)
    rows = list(csv.DictReader(out.open()))
    assert rows[0]["decision"] == "approve"
    assert float(rows[0]["sent_s"]) >= 0   # when it was sent, for measuring part of a run


def test_the_token_is_renewed_before_ingest_would_refuse_it():
    """Payments need a token under 5 minutes old (ingest): renewed after 4, not on a 401."""
    issued = []

    def handler(request):
        issued.append(1)
        return httpx.Response(200, json={"access_token": f"t{len(issued)}"})

    now = [0.0]

    async def run():
        async with httpx.AsyncClient(base_url="http://kong",
                                     transport=httpx.MockTransport(handler)) as client:
            token = sender.Token(client, CREDS, clock=lambda: now[0])
            first = await token.current()
            now[0] = 200
            same = await token.current()
            now[0] = 250
            renewed = await token.current()
            return first, same, renewed

    assert asyncio.run(run()) == ("t1", "t1", "t2")
    assert sender.FRESH_FOR < 300
