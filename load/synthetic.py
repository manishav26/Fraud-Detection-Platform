"""Fake payments at a fixed rate, for load tests (week 2, step 4). Needs no Kaggle data.

    python -m load.synthetic --rps 100 --seconds 60
    make load RPS=100 SECONDS=60

Payments are random but shaped like the real ones: a pool of users (so history builds up),
amounts from a log-normal (many small, a few large) and a handful of the 51 attrs filled in.
Each run is reproducible (--seed), and txn ids include the seed so runs never collide.
"""

import argparse
import asyncio
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

from load.credentials import LOAD_CLIENT, credentials
from load.sender import run, write_csv

START = datetime(2026, 1, 1, tzinfo=UTC)


def fake_request(i, rng, users, seed):
    return {
        "txn_id": f"syn-{seed}-{i}",
        "user_id": f"syn-user-{rng.randrange(users)}",
        "amount": round(min(rng.lognormvariate(4, 1.2), 30_000), 2),   # median about $55
        "ts": (START + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "channel": rng.choice(["desktop", "mobile", None]),
        "attrs": {
            "card1": rng.randrange(1000, 18400),
            "card4": rng.choice(["visa", "mastercard", "discover", "american express"]),
            "card6": rng.choice(["debit", "credit"]),
            "addr1": rng.choice([204.0, 299.0, 325.0, None]),
            "ProductCD": rng.choice("WCHRS"),
            "P_emaildomain": rng.choice(["gmail.com", "yahoo.com", "hotmail.com", None]),
            "C1": float(rng.randrange(1, 5)),
            "D1": rng.choice([0.0, float(rng.randrange(1, 600)), None]),
        },
    }


def schedule(rps, seconds, users, seed):
    """Evenly spaced: payment i is sent at i / rps seconds."""
    rng = random.Random(seed)
    for i in range(int(rps * seconds)):
        yield i / rps, fake_request(i, rng, users, seed), None


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--rps", type=float, default=50, help="payments per second")
    p.add_argument("--seconds", type=float, default=30)
    p.add_argument("--users", type=int, default=5000)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--concurrency", type=int, default=64)
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--client", default=LOAD_CLIENT, help="send as this client (docs/access.md)")
    p.add_argument("--out", help="also save one row per payment here (CSV, as load/replay.py)")
    args = p.parse_args()

    print(f"Sending {int(args.rps * args.seconds):,} fake payments at {args.rps:g}/s"
          f" as {args.client}")
    stats, wall = asyncio.run(run(schedule(args.rps, args.seconds, args.users, args.seed),
                                  args.url, args.concurrency, keep_rows=bool(args.out),
                                  creds=credentials(args.client)))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        write_csv(stats.rows, args.out)
    print(stats.summary(wall))


if __name__ == "__main__":
    main()
