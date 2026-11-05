# Model card: fraud-lgbm-v1

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
n_estimators 1200, learning_rate 0.0777, num_leaves 31, min_child_samples 20, subsample 0.85, subsample_freq 1, colsample_bytree 0.54, reg_lambda 2.1408, random_state 42,
LightGBM 4.7.0. Settings from random search (docs/results/tuning.md).

## Results (test days 92-109)
| Metric | Value |
|---|---|
| PR-AUC | 0.4966 |
| Recall @ 1% FPR | 0.4320 |
| Fraud rate | 0.0351 |
| Approve / review / decline share | 98.7% / 0.2% / 1.1% |

Decision thresholds (from `config/decision.yaml`): review >= 0.55,
decline >= 0.7.

## Speed (one transaction at a time, 1000 requests, laptop)
P50 0.08 ms, P95 0.15 ms, P99 0.16 ms. Budget: 5 ms.

## Files and integrity
Saved in `models/fraud-lgbm-v1/` (not in git). SHA-256 checked on every load:

| File | SHA-256 |
|---|---|
| model.txt | `26eff82aa44247b50c376cb3159a141098c6679711ebfe2fbfaaa0e1cfcf66f6` |
| meta.json | `0e16ee8c1bace56ed234175a7c1cb5e19a70015ccf1d890bb07789ccc9f083ef` |

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
