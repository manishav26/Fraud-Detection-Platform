"""Step 7: train the v1 model, package it, measure speed, and write the model card.

Same setup as calibrate.py: LightGBM on days 1-73, isotonic calibration on days 74-91,
everything reported on days 92-109. Saves to models/fraud-lgbm-v1/ (not committed).
"""

import json
import time
from pathlib import Path

import lightgbm
import numpy as np
import yaml
from sklearn.metrics import average_precision_score, roc_curve

from ml.model import FraudModel
from ml.train.calibrate import (
    FEATURES,
    TARGET,
    fit_calibrator,
    load_data,
    to_model_input,
    train_model,
)

ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = ROOT / "models" / "fraud-lgbm-v1"
CARD = ROOT / "docs" / "model_card.md"


def recall_at_fpr(y, s, fpr_target=0.01):
    fpr, tpr, _ = roc_curve(y, s)
    return float(np.interp(fpr_target, fpr, tpr))


def main():
    thresholds = yaml.safe_load((ROOT / "config" / "decision.yaml").read_text())
    print("loading data and building features...")
    df = load_data()
    train, val = df[df["split"] == "train"], df[df["split"] == "val"]
    mid_day = val["TransactionDT"].min() // 86400 + 18
    cal = val[val["TransactionDT"] // 86400 < mid_day]
    test = val[val["TransactionDT"] // 86400 >= mid_day]

    print("training LightGBM...")
    lgbm, categories = train_model(train)
    calibrator = fit_calibrator(
        lgbm.predict_proba(to_model_input(cal, categories)[0])[:, 1], cal[TARGET])

    info = {"name": "fraud-lgbm-v1", "train_days": "1-73", "calibration_days": "74-91",
            "test_days": "92-109", "n_features": len(FEATURES),
            "lightgbm": lightgbm.__version__}
    model = FraudModel.from_training(lgbm, categories, calibrator,
                                     {k: float(v) for k, v in thresholds.items()}, info)
    manifest = model.save(MODEL_DIR)
    print(f"saved {MODEL_DIR}")

    # reload from disk, exactly as the scoring service would
    loaded = FraudModel.load(MODEL_DIR)
    probs = loaded.predict_frame(test)
    y = test[TARGET].to_numpy()
    direct = calibrator.predict(lgbm.predict_proba(to_model_input(test, categories)[0])[:, 1])
    max_diff = float(np.max(np.abs(probs - direct)))
    print(f"loaded model matches the in-memory model: max difference {max_diff:.2e}")

    pr_auc = average_precision_score(y, probs)
    recall = recall_at_fpr(y, probs)
    decisions = [loaded.decide(p) for p in probs]
    share = {d: decisions.count(d) / len(decisions) for d in ["approve", "review", "decline"]}

    # speed: one request at a time, like the live service
    sample = test.sample(1000, random_state=0)
    rows = sample[FEATURES].to_dict("records")
    for r in rows[:50]:
        loaded.predict_rows([r])                      # warm-up
    times = []
    for r in rows:
        start = time.perf_counter()
        loaded.predict_rows([r])
        times.append((time.perf_counter() - start) * 1000)
    p50, p95, p99 = np.percentile(times, [50, 95, 99])
    print(f"latency per transaction: P50 {p50:.2f} ms, P95 {p95:.2f} ms, P99 {p99:.2f} ms")
    print(f"test days 92-109: PR-AUC {pr_auc:.4f}, recall @ 1% FPR {recall:.4f}")

    CARD.write_text(f"""# Model card: fraud-lgbm-v1

## What it is
LightGBM classifier with isotonic calibration. Input: the 65 features in
`features/schema.yaml` (51 raw IEEE-CIS attrs with raw D values, `log_amount`, `hour`,
12 per-user history features). Output: calibrated fraud probability (0-1).

## Data
| Part | Days | Use |
|---|---|---|
| Train | 1-73 | LightGBM fit |
| Calibration | 74-91 | isotonic calibration |
| Test | 92-109 | all numbers below |

## Settings
n_estimators 400, learning_rate 0.05, num_leaves 64, random_state 42,
LightGBM {info['lightgbm']}. No tuning yet (week 3).

## Results (test days 92-109)
| Metric | Value |
|---|---|
| PR-AUC | {pr_auc:.4f} |
| Recall @ 1% FPR | {recall:.4f} |
| Fraud rate | {y.mean():.4f} |
| Approve / review / decline share | {share['approve']:.1%} / {share['review']:.1%} / {share['decline']:.1%} |

Decision thresholds (from `config/decision.yaml`): review >= {thresholds['review_threshold']},
decline >= {thresholds['decline_threshold']}.

## Speed (one transaction at a time, {len(times)} requests, laptop)
P50 {p50:.2f} ms, P95 {p95:.2f} ms, P99 {p99:.2f} ms. Budget: 5 ms.

## Files and integrity
Saved in `models/fraud-lgbm-v1/` (not in git). SHA-256 checked on every load:

| File | SHA-256 |
|---|---|
| model.txt | `{manifest['model.txt']}` |
| meta.json | `{manifest['meta.json']}` |

## How to use (scoring service)
```python
from ml.model import FraudModel
model = FraudModel.load("models/fraud-lgbm-v1")
prob = model.predict_one(request, history)   # request: amount, ts, attrs; history: dict or None
decision = model.decide(prob)               # approve / review / decline
```
`hour` is taken from `ts` in UTC, so the replay must build `ts` as a fixed midnight-UTC start
date + `TransactionDT` seconds. Unknown categories and missing values are treated as missing.

## Limits
Single run, one seed, untuned. Proxy user id (not a real customer id). Raw D columns
(normalized D tested in docs/results/e2_dnorm.md, not adopted yet).
""")
    print(f"saved {CARD}")
    print(json.dumps({"p95_ms": round(p95, 3), "budget_ms": 5}, indent=None))


if __name__ == "__main__":
    main()