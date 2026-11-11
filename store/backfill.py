"""Load past payments (days 1-109) into Cassandra, so live users have their real history.

    make backfill

Writes straight to Cassandra, not through the gateway: these are old payments, so they must not be
scored or appear in txn.raw as new ones. Safe to run again: a row with the same user, time and
txn id is overwritten. Design: docs/cassandra.md
"""

import argparse
import time
from pathlib import Path

import pandas as pd
from cassandra.concurrent import execute_concurrent_with_args

from features.proxy_id import add_proxy_id
from load.replay import clean, to_ts
from pseudonym import pseudonym
from store.history import COLUMNS, HistoryStore, is_known, row_values

ROOT = Path(__file__).resolve().parents[1]
FILES = [ROOT / "data" / "processed" / "train.parquet", ROOT / "data" / "processed" / "val.parquet"]
CHUNK = 10_000
CONCURRENCY = 64
VERIFY_USERS = 1_000
# What history needs, plus what the proxy user id is built from
READ = ["TransactionID", "TransactionDT", "TransactionAmt", "card1", "addr1", "D1", *COLUMNS]


def load_known(files):
    """Past payments of known users only: scoring doesn't store or read the unknown ones."""
    df = pd.concat((pd.read_parquet(f, columns=READ) for f in files), ignore_index=True)
    return known_rows(df)


def known_rows(df):
    """Known users only, under their pseudonyms, exactly as ingest stores them (docs/privacy.md):
    the history rows must match the ids live payments arrive with. Needs the cluster's key
    (PSEUDONYM_KEY, via scripts/k8s_secrets.sh run); compose uses the dev key."""
    df = add_proxy_id(df)
    df = df[df["uid"].map(is_known)].reset_index(drop=True)
    df["uid"] = df["uid"].map(pseudonym)
    return df


def to_values(row):
    """One data row -> INSERT values. Same user id and timeline as load/replay.py."""
    return row_values({"txn_id": str(row["TransactionID"]), "user_id": row["uid"],
                       "amount": row["TransactionAmt"], "ts": to_ts(row["TransactionDT"]),
                       "attrs": {c: clean(row[c]) for c in COLUMNS}})


def write_all(store, df):
    start = time.perf_counter()
    for i in range(0, len(df), CHUNK):
        values = [to_values(r) for r in df.iloc[i:i + CHUNK].to_dict("records")]
        execute_concurrent_with_args(store.session, store.insert, values, concurrency=CONCURRENCY)
        done = min(i + CHUNK, len(df))
        print(f"  {done:,} of {len(df):,} ({done / (time.perf_counter() - start):,.0f} rows/s)",
              flush=True)


def verify(store, df, users=VERIFY_USERS, seed=0):
    """Compare stored and expected counts for a sample of users. Returns the mismatches.

    Reads only up to the end of this data, so rows a replay added later don't count. A full
    COUNT(*) would scan the whole table, and wouldn't show if one user's rows were wrong.
    """
    expected = df.groupby("uid").size()
    sample = expected.sample(min(users, len(expected)), random_state=seed)
    end = to_ts(df["TransactionDT"].max() + 1)
    return [(user, want, got) for user, want in sample.items()
            if (got := len(store.past(user, end))) != want]


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--verify-only", action="store_true", help="only compare counts")
    args = p.parse_args()

    df = load_known(FILES)
    store = HistoryStore()
    if not args.verify_only:
        print(f"Writing {len(df):,} payments of known users")
        write_all(store, df)
    wrong = verify(store, df)
    store.close()
    if wrong:
        raise SystemExit(f"FAILED: {len(wrong)} users differ, first: {wrong[:3]}")
    print(f"OK: stored counts match for {min(VERIFY_USERS, df['uid'].nunique()):,} sampled users")


if __name__ == "__main__":
    main()
