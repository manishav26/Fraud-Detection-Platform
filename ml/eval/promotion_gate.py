"""Promotion gate: may a candidate model replace the live one?

Both models are scored on the golden set, test days 92-109, which v1 never saw (trained on
1-73, calibrated on 74-91). The candidate passes only if all three checks pass:
  1. better  PR-AUC at least MIN_GAIN (0.01) above the current model; smaller gains are
             within seed noise (E1), so not worth a swap
  2. fast    score + reasons for one payment, p95 at most MAX_MS (5 ms), one thread
  3. safe    share of payments flagged (review + decline) within analyst capacity (2%)

Run it before `make promote`. Exit code 0 = promote, 1 = keep the current model.

    python -m ml.eval.promotion_gate --candidate models/fraud-lgbm-v1-tuned \
                                     --current models/fraud-lgbm-v1
Writes docs/results/promotion_gate.md.

Which days: the candidate's own test days, from its meta.json (`info.test_days`): 92-109 for
v1, a later window for a model retrained on recent days (as in E3), which has seen 92-109.
Both models are always judged on the same days. --test-days overrides it.
"""

import argparse
import sys
import time

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score

from ml.model import FraudModel
from ml.train.calibrate import RESULTS, REVIEW_CAPACITY, TARGET, load_data

MIN_GAIN = 0.01
MAX_MS = 5.0
TIMING_SAMPLE = 500
WARM_UP = 50


GOLDEN_DAYS = (92, 109)


def parse_days(text):
    """'92-109' -> (92, 109)."""
    first, last = (int(x) for x in str(text).split("-"))
    if first > last:
        raise ValueError(f"days {text}: first day after last day")
    return first, last


def held_out_days(model):
    """The days a model was held out on (its meta.json info.test_days), else 92-109."""
    days = (model.meta.get("info") or {}).get("test_days")
    return parse_days(days) if days else GOLDEN_DAYS


def days_seen(model):
    """[(what, (first, last))]: the days a model was trained and calibrated on (its meta.json)."""
    info = model.meta.get("info") or {}
    return [(what, parse_days(info[key])) for what, key in
            (("trained", "train_days"), ("calibrated", "calibration_days")) if info.get(key)]


def unseen(days, candidate, current):
    """The fourth check: the test days must be unseen by both models. The test days come from the
    candidate's own meta.json, so without this a candidate could name days it trained on, or easy
    ones, and be judged on them (threat 10). A candidate that doesn't record its own training and
    calibration days can't show this, so it fails; the current model is checked when it records
    them (v1 does)."""
    first, last = days
    problems = [f"{name} {what} on {a}-{b}"
                for name, model in (("candidate", candidate), ("current", current))
                for what, (a, b) in days_seen(model) if a <= last and first <= b]
    if len(days_seen(candidate)) < 2:
        problems.append("candidate doesn't record its training and calibration days")
    return {"check": "unseen", "value": "; ".join(problems) or f"days {first}-{last}",
            "rule": "not trained or calibrated on", "passed": not problems}


def golden_set(df, days=GOLDEN_DAYS):
    """The payments on days first-last (inclusive), by TransactionDT day."""
    first, last = days
    day = df["TransactionDT"] // 86400
    return df[(day >= first) & (day <= last)]


def measure(model, golden, sample=TIMING_SAMPLE, seed=0):
    """PR-AUC, flagged share and per-payment latency (score + reasons) of one model."""
    rows = golden[model.features].to_dict("records")
    y = golden[TARGET].to_numpy()
    probs = model.predict_rows(rows)
    flagged = probs >= model.meta["thresholds"]["review_threshold"]

    rng = np.random.default_rng(seed)
    picked = [rows[i] for i in rng.choice(len(rows), min(sample, len(rows)), replace=False)]
    for r in picked[:WARM_UP]:
        model.predict_rows([r])
        model.explain_rows([r])
    ms = []
    for r in picked:
        start = time.perf_counter()
        model.predict_rows([r])
        model.explain_rows([r])
        ms.append((time.perf_counter() - start) * 1000)
    return {"PR-AUC": float(average_precision_score(y, probs)),
            "flagged share": float(flagged.mean()),
            "p95 ms": float(np.percentile(ms, 95))}


def check(candidate, current, min_gain=MIN_GAIN, max_ms=MAX_MS, capacity=REVIEW_CAPACITY):
    """Apply the three rules to two `measure` results. Returns (passed, table rows)."""
    gain = candidate["PR-AUC"] - current["PR-AUC"]
    rows = [
        {"check": "better", "value": f"{gain:+.4f} PR-AUC",
         "rule": f">= +{min_gain}", "passed": gain >= min_gain},
        {"check": "fast", "value": f"{candidate['p95 ms']:.2f} ms",
         "rule": f"<= {max_ms} ms", "passed": candidate["p95 ms"] <= max_ms},
        {"check": "safe", "value": f"{candidate['flagged share']:.2%} flagged",
         "rule": f"<= {capacity:.0%}", "passed": candidate["flagged share"] <= capacity},
    ]
    return all(r["passed"] for r in rows), rows


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--candidate", required=True, help="folder of the model to promote")
    p.add_argument("--current", required=True, help="folder of the live model")
    p.add_argument("--test-days", help="e.g. 169-182; default: the candidate's own test days")
    args = p.parse_args()

    candidate_model = FraudModel.load(args.candidate)
    current_model = FraudModel.load(args.current)
    days = parse_days(args.test_days) if args.test_days else held_out_days(candidate_model)
    print("loading data and building features...")
    golden = golden_set(load_data(), days)
    print(f"test days {days[0]}-{days[1]}: {len(golden):,} payments, "
          f"{int(golden[TARGET].sum()):,} frauds")

    cand, curr = measure(candidate_model, golden), measure(current_model, golden)
    passed, rows = check(cand, curr)
    rows.append(unseen(days, candidate_model, current_model))
    passed = passed and rows[-1]["passed"]
    models = pd.DataFrame([{"model": "candidate", "folder": args.candidate, **cand},
                           {"model": "current", "folder": args.current, **curr}]).round(4)
    checks = pd.DataFrame(rows)
    verdict = ("PROMOTE: the candidate passes every check." if passed else
               "KEEP the current model: " + ", ".join(r["check"] for r in rows
                                                      if not r["passed"]) + " failed.")
    print("\n", models.to_string(index=False))
    print("\n", checks.to_string(index=False))
    print(f"\n{verdict}")

    RESULTS.mkdir(parents=True, exist_ok=True)
    with open(RESULTS / "promotion_gate.md", "w") as f:
        f.write("# Promotion gate\n\n")
        f.write(f"Test days {days[0]}-{days[1]} ({len(golden):,} payments). Rules: PR-AUC at "
                f"least +{MIN_GAIN} over the current model, score + reasons p95 at most "
                f"{MAX_MS} ms (one payment at a time, one thread), flagged share within "
                f"{REVIEW_CAPACITY:.0%}, test days unseen by both models.\n\n")
        f.write(f"**{verdict}**\n\n")
        f.write(models.to_markdown(index=False) + "\n\n")
        f.write(checks.to_markdown(index=False) + "\n")
    print("saved docs/results/promotion_gate.md")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()