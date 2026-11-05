"""How long do history reads take, especially for the users with the most payments?

    make read-timing        (needs `make backfill` first)

Sets the read time limit from evidence (CASSANDRA_READ_TIMEOUT_MS): too tight, and the heaviest
users would always be scored without history. Run from the laptop, so times include the trip to
Docker: slightly slower than scoring sees.
"""

import time

import numpy as np

from load.replay import to_ts
from store.backfill import FILES, load_known
from store.history import HistoryStore

GROUPS = [("100 random users", 100, False), ("20 largest users", 20, True)]
ROUNDS = 5   # each user is read this many times
LIMITS_MS = (50, 100, 200)


def timed_reads(store, users, end):
    out = []
    for _ in range(ROUNDS):
        for user in users:
            t0 = time.perf_counter()
            store.past(user, end)
            out.append((time.perf_counter() - t0) * 1000)
    return out


def main():
    df = load_known(FILES)
    sizes = df.groupby("uid").size().sort_values(ascending=False)
    end = to_ts(df["TransactionDT"].max() + 1)
    store = HistoryStore()
    timed_reads(store, sizes.index[:5], end)   # warm-up

    print("| Users | Payments per user | Reads | p50 ms | p95 ms | p99 ms | max ms |"
          " Over 50 / 100 / 200 ms |")
    print("|---|---|---:|---:|---:|---:|---:|---:|")
    for name, n, largest in GROUPS:
        chosen = sizes.head(n) if largest else sizes.sample(n, random_state=0)
        ms = timed_reads(store, chosen.index, end)
        p50, p95, p99 = np.percentile(ms, [50, 95, 99])
        over = " / ".join(str(sum(m > limit for m in ms)) for limit in LIMITS_MS)
        print(f"| {name} | {chosen.min()} to {chosen.max()} | {len(ms):,} | {p50:.1f} | {p95:.1f} |"
              f" {p99:.1f} | {max(ms):.1f} | {over} |")
    store.close()


if __name__ == "__main__":
    main()
