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

Note: a model retrained on recent days (as in E3) has seen days 92-109, so for it the golden
set must be a newer window it was not trained on.
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


def golden_set(df):
    """Test days 92-109: the second half of the validation period."""
    val = df[df["split"] == "val"]
    mid_day = val["TransactionDT"].min() // 86400 + 18
    return val[val["TransactionDT"] // 86400 >= mid_day]


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
    args = p.parse_args()

    candidate_model = FraudModel.load(args.candidate)
    current_model = FraudModel.load(args.current)
    print("loading data and building features...")
    golden = golden_set(load_data())
    print(f"golden set: {len(golden):,} payments, {int(golden[TARGET].sum()):,} frauds")

    cand, curr = measure(candidate_model, golden), measure(current_model, golden)
    passed, rows = check(cand, curr)
    models = pd.DataFrame([{"model": "candidate", "folder": args.candidate, **cand},
                           {"model": "current", "folder": args.current, **curr}]).round(4)
    checks = pd.DataFrame(rows)
    verdict = ("PROMOTE: the candidate passes all three checks." if passed else
               "KEEP the current model: " + ", ".join(r["check"] for r in rows
                                                      if not r["passed"]) + " failed.")
    print("\n", models.to_string(index=False))
    print("\n", checks.to_string(index=False))
    print(f"\n{verdict}")

    RESULTS.mkdir(parents=True, exist_ok=True)
    with open(RESULTS / "promotion_gate.md", "w") as f:
        f.write("# Promotion gate\n\n")
        f.write(f"Golden set: test days 92-109 ({len(golden):,} payments). Rules: PR-AUC at "
                f"least +{MIN_GAIN} over the current model, score + reasons p95 at most "
                f"{MAX_MS} ms (one payment at a time, one thread), flagged share within "
                f"{REVIEW_CAPACITY:.0%}.\n\n")
        f.write(f"**{verdict}**\n\n")
        f.write(models.to_markdown(index=False) + "\n\n")
        f.write(checks.to_markdown(index=False) + "\n")
    print("saved docs/results/promotion_gate.md")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()