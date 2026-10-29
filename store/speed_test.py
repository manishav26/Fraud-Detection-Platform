"""Cassandra speed test (week 2, step 2). Run: make cassandra-speed

Writes 10,000 payments for 1,000 test users, then times one-at-a-time writes and reads the way
scoring-svc will use them. Test users start with "speedtest-" and are deleted at the end.
Prints a Markdown table for docs/results/cassandra.md.
"""

import random
import time
from datetime import UTC, datetime, timedelta

import numpy as np
from cassandra.concurrent import execute_concurrent_with_args

from store.history import HistoryStore, row_values

N_WRITES, N_USERS, N_TIMED = 10_000, 1_000, 1_000
START = datetime(2026, 1, 1, tzinfo=UTC)


def fake_txns():
    rng = random.Random(7)
    return [{"txn_id": f"speedtest-{i}", "user_id": f"speedtest-{i % N_USERS}",
             "amount": round(rng.uniform(1, 500), 2), "ts": START + timedelta(minutes=i),
             "attrs": {"P_emaildomain": rng.choice(["gmail.com", "yahoo.com", None]),
                       "DeviceType": rng.choice(["desktop", "mobile"]),
                       "ProductCD": rng.choice("WCHRS")}}
            for i in range(N_WRITES)]


def timed(fn, items):
    """Milliseconds per call."""
    out = []
    for item in items:
        t0 = time.perf_counter()
        fn(item)
        out.append((time.perf_counter() - t0) * 1000)
    return out


def row(name, ms):
    p50, p99 = np.percentile(ms, [50, 99])
    return f"| {name} | {len(ms):,} | {p50:.2f} | {p99:.2f} |"


def main():
    store = HistoryStore()
    txns = fake_txns()

    # 1. Bulk load: 64 writes in flight at once, like many scoring pods writing together.
    t0 = time.perf_counter()
    execute_concurrent_with_args(store.session, store.insert, [row_values(t) for t in txns],
                                 concurrency=64)
    bulk_secs = time.perf_counter() - t0

    # 2. One at a time, as one scoring request does. These rows already exist, so this
    #    also proves upserts: writing them again must not add rows.
    writes = timed(store.save, txns[:N_TIMED])
    reads = timed(lambda t: store.past(t["user_id"], t["ts"]), txns[-N_TIMED:])

    rows_per_user = len(store.past("speedtest-0", START + timedelta(days=365)))
    assert rows_per_user == N_WRITES // N_USERS, f"expected 10 rows, got {rows_per_user}"

    delete = store.session.prepare("DELETE FROM txn_by_user WHERE user_id = ?")
    execute_concurrent_with_args(store.session, delete,
                                 [(f"speedtest-{u}",) for u in range(N_USERS)])
    store.close()

    print(f"Bulk load: {N_WRITES:,} writes in {bulk_secs:.2f} s "
          f"({N_WRITES / bulk_secs:,.0f} writes/s, 64 in flight)\n")
    print("| Operation | Calls | p50 ms | p99 ms |")
    print("|---|---|---|---|")
    print(row("Write one payment (upsert)", writes))
    print(row("Read one user's earlier payments", reads))
    print(f"\nUpsert check: user speedtest-0 has {rows_per_user} rows after rewrites (expected 10)")


if __name__ == "__main__":
    main()
