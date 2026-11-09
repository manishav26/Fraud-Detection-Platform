"""E6 live: probe the API with variants of a flagged fraud payment (defenses D1 and D3).

    make k8s-probe-attack            probe detection on, as deployed
    make k8s-probe-attack D3=off     the same attack with probe detection off, then back on

E6's attacker (ml/experiments/e6_evasion.py), against the real platform instead of the model.
As merchant-1, a client, so it sees only the decision (D1). For each of --cards fraud cards from
the stream data (days 110-182), in a random order:
  1. Send the fraud payment again (a new txn_id). Approved -> the model doesn't flag it: next card.
  2. Send --variants variants on the same card, 40 minutes apart, so the hour changes and all fall
     within 24 h. Each changes one field the attacker controls: the amount (50-100% of the
     original), the e-mail domains, the device, the card brand or type. A change is kept if the
     answer gets better (decline -> review), as E6's attacker does when it sees only decisions.

Pass with D3 on: once a card has had 5 flagged answers, no later variant is approved. Approvals
before that are possible, as in E6 (17.5% of attacks). With D3 off, some later variants may be
approved: that is what D3 stops.

Also checks D1: every answer to merchant-1 has only txn_id and decision, including one where
merchant-1 sends `X-Consumer-Groups: tester` itself (Kong replaces it).

Each run uses new txn_ids; cards are picked with --seed (default: the time), so runs don't attack
a card an earlier run already flagged. Prints a Markdown table for docs/results/e6_live.md.
"""

import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from load.credentials import credentials
from load.replay import attr_columns, clean, load_rows, to_request
from store.history import is_known
from store.probe import PROBE_FLAGS

ATTACKER = "merchant-1"
FIELDS = ["amount", "P_emaildomain", "R_emaildomain", "DeviceType", "card4", "card6"]
MIN_AMOUNT_SHARE = 0.5   # as in E6: the attacker still wants at least half of the money
SPACING = timedelta(minutes=40)
LEVEL = {"approve": 0, "review": 1, "decline": 2}
SHORT = {"txn_id", "decision"}   # all a client may see (D1)


def value_pools(df):
    """Values the attacker may pick: what real customers send, as often as they send them."""
    return {f: [clean(v) for v in df[f].to_numpy()] for f in FIELDS if f != "amount"}


def mutate(req, original, rng, pools):
    """Change one controllable field (the amount stays 50-100% of the original's)."""
    new = {**req, "attrs": dict(req["attrs"])}
    field = FIELDS[rng.integers(len(FIELDS))]
    if field == "amount":
        new["amount"] = round(original["amount"] * rng.uniform(MIN_AMOUNT_SHARE, 1), 2)
    else:
        value = pools[field][rng.integers(len(pools[field]))]
        new["attrs"][field] = value
        if field == "DeviceType":
            new["channel"] = value
    return new


def at(req, txn_id, ts):
    return {**req, "txn_id": txn_id, "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ")}


def attack_card(send, original, rng, pools, variants, prefix):
    """One card. Returns its row for the table, or None if the model doesn't flag the payment."""
    start = datetime.fromisoformat(original["ts"]) + timedelta(seconds=1)
    first = send(at(original, f"{prefix}-0", start))
    if first == "approve":
        return None
    best, best_level = original, LEVEL[first]
    flags, approved, approved_after, first_approval = 1, 0, 0, None
    for q in range(1, variants + 1):
        tried = mutate(best, original, rng, pools)
        decision = send(at(tried, f"{prefix}-{q}", start + q * SPACING))
        if decision == "approve":
            approved += 1
            first_approval = first_approval or q
            approved_after += flags >= PROBE_FLAGS
        else:
            flags += 1
        if LEVEL[decision] < best_level:
            best, best_level = tried, LEVEL[decision]
    return {"card": original["user_id"], "first": first, "flags": flags, "approved": approved,
            "first_approval": first_approval, "approved_after": approved_after}


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--url", default="http://localhost:8080")
    p.add_argument("--data", default=ROOT / "data" / "processed" / "stream.parquet")
    p.add_argument("--cards", type=int, default=5, help="flagged fraud cards to attack")
    p.add_argument("--variants", type=int, default=30, help="variants per card")
    p.add_argument("--seed", type=int, default=int(time.time()))
    p.add_argument("--d3", default="on", help="on or off: what scoring runs (for the report)")
    args = p.parse_args()

    df = load_rows(args.data)
    frauds = df[(df["isFraud"] == 1) & df["uid"].map(is_known)]
    attrs, pools = attr_columns(), value_pools(df)
    rng = np.random.default_rng(args.seed)
    order = rng.permutation(len(frauds))
    print(f"{len(frauds):,} fraud payments on known cards; seed {args.seed}; "
          f"probe detection {args.d3}")

    leaks = []
    with httpx.Client(base_url=args.url, timeout=15) as client:
        r = client.post("/token", json=credentials(ATTACKER))
        r.raise_for_status()
        auth = {"Authorization": f"Bearer {r.json()['access_token']}"}

        def send(req, extra=None):
            r = client.post("/v1/transactions/score", json=req, headers={**auth, **(extra or {})})
            r.raise_for_status()
            body = r.json()
            if set(body) != SHORT:
                leaks.append(sorted(body))
            return body["decision"]

        rows, tried = [], 0
        for i in order:
            if len(rows) == args.cards:
                break
            tried += 1
            original = to_request(frauds.iloc[i].to_dict(), attrs)
            row = attack_card(send, original, rng, pools, args.variants,
                              f"probe{args.seed}-{tried}")
            if row:
                rows.append(row)
                print(f"  card {len(rows)}: {row['flags']} flagged, {row['approved']} approved "
                      f"({row['approved_after']} after {PROBE_FLAGS} flags)")
        # D1 again: a client claiming the tester role still gets the short answer
        forged = to_request(frauds.iloc[order[0]].to_dict(), attrs)
        send({**forged, "txn_id": f"probe{args.seed}-forged"}, {"X-Consumer-Groups": "tester"})

    if not rows:
        sys.exit(f"none of the {tried} fraud payments tried was flagged: nothing to attack")
    print(f"\n{tried - len(rows)} fraud payments approved on the first try were skipped\n")
    print(f"| Card | First answer | Flagged | Approved | First approval (query) | "
          f"Approved after {PROBE_FLAGS} flags |")
    print("|---|---|---|---|---|---|")
    for n, r in enumerate(rows, 1):
        print(f"| {n} | {r['first']} | {r['flags']} | {r['approved']} | "
              f"{r['first_approval'] or '-'} | {r['approved_after']} |")
    after = sum(r["approved_after"] for r in rows)
    print(f"\nProbe detection {args.d3}: {sum(r['first_approval'] is not None for r in rows)} of "
          f"{len(rows)} cards got an approval; {after} approvals after {PROBE_FLAGS} flags")
    print(f"D1: {'every answer had only txn_id and decision' if not leaks else f'LEAKED {leaks[0]}'}")

    failed = bool(leaks) or (args.d3 == "on" and after > 0)
    if failed:
        sys.exit("E6 LIVE FAILED")
    print("E6 LIVE PASSED" if args.d3 == "on" else "(D3 off: shown for comparison)")


if __name__ == "__main__":
    main()
