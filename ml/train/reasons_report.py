"""Reasons (TreeSHAP) on the real model: speed per payment, and what the reasons look like.

Uses the packaged model in models/fraud-lgbm-v1 and the test days 92-109 (as in the model card).

    python -m ml.train.reasons_report      # about 1-2 minutes
"""

import time
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

from ml.model import FraudModel
from ml.train.calibrate import RESULTS, TARGET, load_data

ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = ROOT / "models" / "fraud-lgbm-v1"
BUDGET_MS = 10


def main():
    model = FraudModel.load(MODEL_DIR)
    print("loading data and building features...")
    df = load_data()
    val = df[df["split"] == "val"]
    mid_day = val["TransactionDT"].min() // 86400 + 18
    test = val[val["TransactionDT"] // 86400 >= mid_day]
    rows = test[model.features].to_dict("records")
    y = test[TARGET].to_numpy()
    probs = model.predict_rows(rows)
    decisions = np.array([model.decide(p) for p in probs])
    print(f"test days 92-109: {len(rows):,} payments")

    # speed: one payment at a time, like the live service (score + reasons)
    rng = np.random.default_rng(0)
    sample = [rows[i] for i in rng.choice(len(rows), 1000, replace=False)]
    for r in sample[:50]:
        model.explain_rows([r])                      # warm-up
    score_ms, reason_ms = [], []
    for r in sample:
        start = time.perf_counter()
        model.predict_rows([r])
        mid = time.perf_counter()
        model.explain_rows([r])
        score_ms.append((mid - start) * 1000)
        reason_ms.append((time.perf_counter() - mid) * 1000)
    speed = pd.DataFrame({
        "step": ["score only", "reasons (TreeSHAP)"],
        "P50 ms": [np.percentile(score_ms, 50), np.percentile(reason_ms, 50)],
        "P95 ms": [np.percentile(score_ms, 95), np.percentile(reason_ms, 95)],
        "P99 ms": [np.percentile(score_ms, 99), np.percentile(reason_ms, 99)],
    }).round(2)
    print("\n", speed.to_string(index=False))

    # what the reasons say for flagged payments (review or decline)
    flagged = np.flatnonzero(decisions != "approve")
    reasons = model.explain_rows([rows[i] for i in flagged])
    top1 = Counter(r[0].split("=")[0] for r in reasons if r)
    any3 = Counter(name.split("=")[0] for r in reasons for name in r)
    common = pd.DataFrame({"feature": [f for f, _ in any3.most_common(10)],
                           "in top 3 (flagged)": [any3[f] for f, _ in any3.most_common(10)],
                           "top reason (flagged)": [top1[f] for f, _ in any3.most_common(10)]})
    print("\nmost common reasons for flagged payments:\n", common.to_string(index=False))

    # examples: the 5 highest-risk payments
    examples = []
    for i in np.argsort(-probs)[:5]:
        examples.append({"fraud probability": round(float(probs[i]), 3),
                         "decision": decisions[i], "really fraud": int(y[i]),
                         "reasons": "; ".join(model.explain_rows([rows[i]])[0])})
    examples = pd.DataFrame(examples)
    print("\nhighest-risk examples:\n", examples.to_string(index=False))

    p95 = float(speed.loc[1, "P95 ms"])
    with open(RESULTS / "reasons.md", "w") as f:
        f.write("# Reasons for each decision (TreeSHAP)\n\n")
        f.write("For every payment the model returns the top 3 features that pushed the score "
                "toward fraud, using LightGBM's exact TreeSHAP values (`pred_contrib=True`, no "
                "extra package). Only features that raised the risk are listed, largest first. "
                f"Model `fraud-lgbm-v1`, test days 92-109 ({len(rows):,} payments).\n\n")
        f.write(f"## Speed (one payment at a time, 1,000 payments, laptop; budget {BUDGET_MS} ms)"
                "\n\n")
        f.write(speed.to_markdown(index=False) + "\n\n")
        verdict = "within" if p95 <= BUDGET_MS else "OVER"
        f.write(f"Reasons P95 {p95:.2f} ms: {verdict} the {BUDGET_MS} ms budget.\n\n")
        f.write(f"## Most common reasons ({len(flagged):,} flagged payments: review or decline)"
                "\n\n")
        f.write(common.to_markdown(index=False) + "\n\n")
        f.write("## Highest-risk examples\n\n")
        f.write(examples.to_markdown(index=False) + "\n\n")
        f.write("The number after each reason is its SHAP value in log-odds: how much that "
                "feature raised the model's raw score for this payment.\n")
    print(f"\nreasons P95 {p95:.2f} ms ({verdict} {BUDGET_MS} ms budget)")
    print("saved docs/results/reasons.md")


if __name__ == "__main__":
    main()