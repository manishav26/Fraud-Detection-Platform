"""Feature job parity: what the job wrote vs the offline training code (week 4, step 1).

The job (Spark or Python) read the first N stream payments from its own topic, starting with
empty state. The reference is features.history.add_history, training's own code, over the same N
payments: a payment's features depend only on earlier payments, which are all among the N.
Target: all 12 features equal on at least 99.9% of payments. Every mismatch is listed.

    make k8s-spark-parity          (loads the topic, runs the job, then this, through a tunnel)
    python -m stream.parity --limit 10000 --since <epoch seconds the run started>
"""

import argparse
import math
import os
from datetime import UTC, datetime

import pandas as pd

from features.history import HISTORY_FEATURES, add_history
from load.replay import load_rows
from stream.sink import COLUMN
from stream.state import to_epoch

TARGET = 0.999


def offline(limit, offset=0):
    df = load_rows(os.path.join("data", "processed", "stream.parquet"), limit, offset)
    df = add_history(df)   # load_rows already added uid and uid_known
    df["txn_id"] = df["TransactionID"].astype(str)
    return df


def read_job_rows(session, df, prefix, since):
    """features_by_txn rows for these payments, written by this run (written_at >= since)."""
    from load.replay import to_ts
    by_day = {}
    for r in df[["txn_id", "TransactionDT"]].itertuples(index=False):
        ts = datetime.fromtimestamp(to_epoch(to_ts(r.TransactionDT)), UTC)
        by_day.setdefault(ts.date(), set()).add(prefix + r.txn_id)
    cols = ", ".join(COLUMN[f] for f in HISTORY_FEATURES)
    query = session.prepare(f"SELECT txn_id, written_at, {cols} FROM features_by_txn WHERE day = ?")
    rows = {}
    for day, wanted in by_day.items():
        for r in session.execute(query, (day,)):
            if r.txn_id in wanted and r.written_at.replace(tzinfo=UTC).timestamp() >= since:
                rows[r.txn_id[len(prefix):]] = {f: getattr(r, COLUMN[f].lower()) for f in HISTORY_FEATURES}
    return rows


def same(a, b):
    a = float("nan") if a is None else float(a)
    b = float("nan") if b is None else float(b)
    if math.isnan(a) or math.isnan(b):
        return math.isnan(a) and math.isnan(b)
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)


def compare(df, job_rows):
    results = []
    for r in df.to_dict("records"):
        got = job_rows.get(r["txn_id"])
        if got is None:
            results.append({"txn_id": r["txn_id"], "status": "missing", "features": "all"})
            continue
        bad = [f for f in HISTORY_FEATURES if not same(got[f], r[f])]
        results.append({"txn_id": r["txn_id"], "status": "equal" if not bad else "differs",
                        "features": ", ".join(bad),
                        "job": ", ".join(f"{got[f]}" for f in bad),
                        "reference": ", ".join(f"{r[f]}" for f in bad)})
    return pd.DataFrame(results)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--limit", type=int, default=10_000)
    p.add_argument("--prefix", default="", help="the txn_id prefix the load used")
    p.add_argument("--since", type=float, default=0, help="only rows written after this time")
    args = p.parse_args()

    from cassandra.cluster import Cluster

    from store.history import auth_provider  # the admin login, through the tunnel

    df = offline(args.limit)
    cluster = Cluster(os.getenv("CASSANDRA_HOSTS", "localhost").split(","), auth_provider=auth_provider())
    try:
        job_rows = read_job_rows(cluster.connect("fraud"), df, args.prefix, args.since)
    finally:
        cluster.shutdown()
    result = compare(df, job_rows)
    counts = result["status"].value_counts().to_dict()
    share = counts.get("equal", 0) / len(result)
    print(f"| Payments | Equal (all 12 features) | Differ | Missing |\n|---|---|---|---|\n"
          f"| {len(result):,} | {counts.get('equal', 0):,} ({share:.3%}) | "
          f"{counts.get('differs', 0):,} | {counts.get('missing', 0):,} |")
    bad = result[result["status"] != "equal"]
    if len(bad):
        print("\nMismatches (first 20):")
        print(bad.head(20).to_string(index=False))
        per_feature = bad["features"].str.split(", ").explode().value_counts()
        print("\nBy feature:\n" + per_feature.to_string())
    verdict = "PASS" if share >= TARGET else "FAIL"
    print(f"\n{verdict}: {share:.3%} of {len(result):,} payments equal (target {TARGET:.1%})")
    raise SystemExit(0 if share >= TARGET else 1)


if __name__ == "__main__":
    main()
