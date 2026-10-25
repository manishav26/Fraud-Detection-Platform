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
n_estimators 400, learning_rate 0.05, num_leaves 64, random_state 42,
LightGBM 3.3.5. No tuning yet (week 3).

## Results (test days 92-109)
| Metric | Value |
|---|---|
| PR-AUC | 0.5040 |
| Recall @ 1% FPR | 0.4278 |
| Fraud rate | 0.0351 |
| Approve / review / decline share | 98.5% / 0.5% / 1.0% |

Decision thresholds (from `config/decision.yaml`): review >= 0.39,
decline >= 0.7.

## Speed (one transaction at a time, 1000 requests, laptop)
P50 0.05 ms, P95 0.05 ms, P99 0.06 ms. Budget: 5 ms.

## Files and integrity
Saved in `models/fraud-lgbm-v1/` (not in git). SHA-256 checked on every load:

| File | SHA-256 |
|---|---|
| model.txt | `2fc249b6fb35254870146599c9f6cf9541e9b2996525b4a7523aed7853ed883f` |
| meta.json | `a34c06cd2a0a227fe30bcab928e7f8d536374b40d15c1de43edfa1a7ce486af6` |

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
