"""Train/serve parity: history features read live from Cassandra vs the offline training code.

Online:  store.past(user, ts) from Cassandra -> history_for_one   (what scoring-svc does)
Offline: add_history over the whole table                        (what training does)
Both come from features/history.py; this checks that the data path between them is right.

Two modes:
  stream  Cassandra holds only the replayed stream payments; offline history is built from the
          stream days too, so both sides start empty at day 110.
  full    Cassandra also holds days 1-109 (make backfill); offline history uses all days,
          exactly as in training.

    make up && make topics && make schema
    make backfill                              # full mode only
    make replay SPEED=0 LIMIT=20000            # check the summary: Degraded must be 0
    python -m ml.eval.parity --mode stream     # or --mode full
"""

import argparse
import math
from datetime import timedelta
from pathlib import Path

import pandas as pd

from features.history import HISTORY_FEATURES, NOVELTY_OF, add_history, history_for_one
from features.proxy_id import add_proxy_id
from load.replay import START
from pseudonym import pseudonym

ROOT = Path(__file__).resolve().parents[2]
PROCESSED = ROOT / "data" / "processed"
RESULTS = ROOT / "docs" / "results"
COLUMNS = ["TransactionID", "TransactionDT", "TransactionAmt", "card1", "addr1", "D1", *NOVELTY_OF]
TARGET_SHARE = 0.999


def plain(v):
    """Missing -> None, as the replay sends it and Cassandra returns it."""
    return None if pd.isna(v) else v


def offline_table(mode):
    splits = ["stream"] if mode == "stream" else ["train", "val", "stream"]
    df = pd.concat([pd.read_parquet(PROCESSED / f"{s}.parquet", columns=COLUMNS)
                    for s in splits], ignore_index=True)
    df = add_history(add_proxy_id(df.sort_values("TransactionDT", kind="stable")))
    # Training counts an earlier payment of the same user in the same second; live scoring
    # reads only ts < now, so it cannot. Those rows are expected to differ.
    df["same_second"] = df.duplicated(["uid", "TransactionDT"], keep="first")
    return df


def replayed_ids(limit):
    """The payments `make replay LIMIT=n` sent: first n stream rows in time order."""
    stream = pd.read_parquet(PROCESSED / "stream.parquet", columns=["TransactionID",
                                                                   "TransactionDT"])
    return set(stream.sort_values("TransactionDT", kind="stable")["TransactionID"].head(limit))


def online_features(rows, store):
    """For each row: read the user's earlier payments from the store, then history_for_one."""
    out = []
    for r in rows.itertuples(index=False):
        ts = START + timedelta(seconds=int(r.TransactionDT))
        past = store.past(pseudonym(r.uid), ts)   # stored under its pseudonym (docs/privacy.md)
        values = {c: plain(getattr(r, c)) for c in NOVELTY_OF}
        out.append(history_for_one(past, ts.timestamp(), float(r.TransactionAmt), values))
    return pd.DataFrame(out, index=rows.index)[HISTORY_FEATURES]


def same_value(a, b):
    if (a is None or (isinstance(a, float) and math.isnan(a))) and \
       (b is None or (isinstance(b, float) and math.isnan(b))):
        return True
    if a is None or b is None or math.isnan(a) or math.isnan(b):
        return False
    return math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6)


def compare(offline, online):
    """Boolean table: True where offline and online agree, one column per feature."""
    equal = pd.DataFrame(index=offline.index, columns=HISTORY_FEATURES, dtype=bool)
    for f in HISTORY_FEATURES:
        equal[f] = [same_value(float(a), float(b)) for a, b in zip(offline[f], online[f])]
    return equal


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--mode", choices=["stream", "full"], required=True)
    p.add_argument("--limit", type=int, default=20000, help="LIMIT used for make replay")
    p.add_argument("--sample", type=int, default=10000)
    args = p.parse_args()

    from store.history import HistoryStore  # only needed for the real run

    print(f"building offline history ({args.mode} mode)...")
    df = offline_table(args.mode)
    ids = replayed_ids(args.limit)
    candidates = df[df["TransactionID"].isin(ids) & df["uid_known"]]
    sample = candidates.sample(min(args.sample, len(candidates)), random_state=0)
    print(f"replayed {len(ids):,} payments, {len(candidates):,} from known users; "
          f"comparing {len(sample):,}")

    store = HistoryStore()
    try:
        online = online_features(sample, store)
    finally:
        store.close()
    equal = compare(sample[HISTORY_FEATURES], online)

    row_ok = equal.all(axis=1)
    same_second = sample["same_second"]
    per_feature = pd.DataFrame({"feature": HISTORY_FEATURES,
                                "equal": [f"{equal[f].mean():.4%}" for f in HISTORY_FEATURES]})
    summary = pd.DataFrame([
        {"rows": "all compared", "count": len(sample), "all 12 features equal":
         f"{row_ok.mean():.4%}"},
        {"rows": "same-second (expected to differ)", "count": int(same_second.sum()),
         "all 12 features equal": f"{row_ok[same_second].mean():.4%}" if same_second.any()
         else "-"},
        {"rows": "all others", "count": int((~same_second).sum()),
         "all 12 features equal": f"{row_ok[~same_second].mean():.4%}"},
    ])
    unexplained = sample[~row_ok & ~same_second]
    passed = row_ok.mean() >= TARGET_SHARE
    verdict = (f"{'PASS' if passed else 'FAIL'}: {row_ok.mean():.4%} of rows equal "
               f"(target {TARGET_SHARE:.1%}); {len(unexplained)} mismatches not explained by "
               "same-second payments.")
    print("\n", summary.to_string(index=False))
    print("\n", per_feature.to_string(index=False))
    print(f"\n{verdict}")

    examples = []
    for idx in unexplained.index[:10]:
        bad = [f for f in HISTORY_FEATURES if not equal.loc[idx, f]]
        examples.append({"TransactionID": int(sample.loc[idx, "TransactionID"]),
                         "features": ", ".join(bad),
                         "offline": ", ".join(f"{sample.loc[idx, f]:.6g}" for f in bad),
                         "online": ", ".join(f"{online.loc[idx, f]:.6g}" for f in bad)})

    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"parity_{args.mode}.md"
    with open(out, "w") as f:
        f.write(f"# Train/serve parity ({args.mode} mode)\n\n")
        what = ("Cassandra holds only the replayed stream payments; offline history is built "
                "from the stream days, so both sides start empty at day 110." if args.mode ==
                "stream" else "Cassandra holds days 1-109 (make backfill) plus the replay; "
                "offline history uses all days, as in training.")
        f.write(f"{what} Online = `store.past()` from Cassandra + `history_for_one`; offline = "
                f"`add_history`. Replay LIMIT {args.limit:,}; {len(sample):,} random payments "
                "of known users compared on all 12 history features (tolerance 1e-6).\n\n")
        f.write(f"**{verdict}**\n\n")
        f.write(summary.to_markdown(index=False) + "\n\n")
        f.write("## Per feature\n\n" + per_feature.to_markdown(index=False) + "\n\n")
        f.write("## Unexplained mismatches (up to 10)\n\n")
        f.write(pd.DataFrame(examples).to_markdown(index=False) + "\n" if examples
                else "None.\n")
        f.write("\nSame-second rows: training counts an earlier payment of the same user in "
                "the same second, live scoring reads only `ts < now`. A replay with "
                "`Degraded` above 0 can also skip history writes, which shows up here as "
                "mismatches.\n")
    print(f"saved {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()