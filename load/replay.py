"""Replay real IEEE-CIS payments through the gateway, in time order, sped up (week 2, step 4).

    python -m load.replay --speed 1000 --limit 5000
    make replay SPEED=1000 LIMIT=5000

Reads data/processed/stream.parquet (days 110-182) by default and turns each row into an API
request:
  txn_id   TransactionID
  user_id  proxy user id v1 (features/proxy_id.py, same as training)
  amount   TransactionAmt
  ts       START (midnight UTC) + TransactionDT seconds, so the model's hour-of-day feature
           matches training: (TransactionDT // 3600) % 24
  channel  DeviceType
  attrs    the 51 raw columns listed in features/schema.yaml (missing -> null)

--speed 1000 means 1000 seconds of data per real second (73 days in about 1.75 hours).
--speed 0 sends as fast as --concurrency allows. --rps 30 instead sends exactly 30 payments per
second, still in time order (load tests: real users, so Cassandra is used, at a fixed rate).
--offset N skips the first N payments, so each load test uses fresh ones. Results per payment, with the true label, go to
data/replay/results.csv (not committed).
"""

import argparse
import asyncio
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import yaml

from features.proxy_id import add_proxy_id
from load.sender import run, write_csv

ROOT = Path(__file__).resolve().parents[1]
START = datetime(2017, 12, 1, tzinfo=UTC)   # any midnight UTC works; this is the usual guess


def attr_columns():
    """The 51 attrs, read from the shared feature contract so the two never drift apart."""
    groups = yaml.safe_load((ROOT / "features" / "schema.yaml").read_text())["attrs"]
    return [c for cols in groups.values() if isinstance(cols, list) for c in cols]


def clean(v):
    """numpy / pandas value -> plain JSON value; NaN -> None."""
    if v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NA:
        return None
    return v.item() if hasattr(v, "item") else v


def to_ts(transaction_dt, start=START):
    return (start + timedelta(seconds=int(transaction_dt))).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_request(row, attrs):
    return {"txn_id": str(row["TransactionID"]), "user_id": row["uid"],
            "amount": float(row["TransactionAmt"]), "ts": to_ts(row["TransactionDT"]),
            "channel": clean(row["DeviceType"]),
            "attrs": {c: clean(row[c]) for c in attrs}}


def schedule(df, speed, chunk=10_000, rps=None):
    """(seconds from start, request, true label) for every row, in time order.

    rps: send evenly at this many payments per second instead of following the data's times.

    Rows are turned into dicts 10,000 at a time, so 210k rows never sit in memory as dicts.
    """
    attrs = attr_columns()
    first = df["TransactionDT"].iloc[0]
    for i in range(0, len(df), chunk):
        for j, row in enumerate(df.iloc[i:i + chunk].to_dict("records"), start=i):
            if rps:
                offset = j / rps
            else:
                offset = (row["TransactionDT"] - first) / speed if speed else 0
            yield offset, to_request(row, attrs), row.get("isFraud")


def load_rows(path, limit=None, offset=0):
    """Only the columns we send (the parquet has 434), sorted by time, with the proxy user id.
    offset: skip that many payments first (the proxy id uses only each row's own values)."""
    cols = ["TransactionID", "TransactionDT", "TransactionAmt", "isFraud", "D1",
            *attr_columns()]
    df = pd.read_parquet(path, columns=list(dict.fromkeys(cols)))
    df = df.sort_values("TransactionDT", kind="stable")
    df = df.iloc[offset:offset + limit] if limit else df.iloc[offset:]
    return add_proxy_id(df).reset_index(drop=True)


def shard(df, spec):
    """'i/N' -> rows i, i+N, i+2N, ... of df (0 <= i < N)."""
    i, n = (int(x) for x in spec.split("/"))
    if not 0 <= i < n:
        raise SystemExit(f"--shard {spec}: needs 0 <= i < N")
    return df.iloc[i::n].reset_index(drop=True)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--data", default=ROOT / "data" / "processed" / "stream.parquet")
    p.add_argument("--speed", type=float, default=1000, help="data seconds per real second; 0 = max")
    p.add_argument("--limit", type=int, help="only the first N payments")
    p.add_argument("--offset", type=int, default=0, help="skip the first N payments")
    p.add_argument("--rps", type=float, help="fixed payments per second (ignores --speed)")
    p.add_argument("--concurrency", type=int, default=32)
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--out", default=ROOT / "data" / "replay" / "results.csv")
    # i/N: only every N-th row starting at row i, so N processes (each at --rps / N) share one block
    # of rows in time order, together sending the whole stream at the full rate (scripts/ramp_test.sh
    # SENDERS: one Python process tops out near 150/s)
    p.add_argument("--shard", default="0/1", help="i/N: send rows i, i+N, i+2N, ... of the block")
    args = p.parse_args()

    df = shard(load_rows(args.data, args.limit, args.offset), args.shard)
    days = (df["TransactionDT"].iloc[-1] - df["TransactionDT"].iloc[0]) / 86400
    pace = f"{args.rps:g} per second" if args.rps else f"speed {args.speed:g}"
    print(f"Replaying {len(df):,} payments ({days:.1f} days of data, from row {args.offset:,})"
          f" at {pace}")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    stats, wall = asyncio.run(run(schedule(df, args.speed, rps=args.rps), args.url, args.concurrency,
                                  keep_rows=True))
    write_csv(stats.rows, args.out)
    print(stats.summary(wall))
    print(f"Per-payment results: {args.out}")


if __name__ == "__main__":
    main()
