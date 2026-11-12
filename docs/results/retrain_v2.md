# Retrained model fraud-lgbm-v2

The retrain E3 does on day 168, packaged like v1. Labels arrive 14 days late, so on day 168 days up to 154 are known.

| Part | Days | Payments |
|---|---|---|
| Train | 95-140 | 138,365 |
| Calibration | 141-154 | 39,992 |
| Test (held out) | 169-182 | 37,693 |

Settings: n_estimators 1200, learning_rate 0.0777, num_leaves 31, min_child_samples 20, subsample 0.85, subsample_freq 1, colsample_bytree 0.54, reg_lambda 2.1408 (tuned v1, ml/configs/lgbm.yaml). Review threshold 0.36 (lowest within 2% on the calibration days), decline 0.7.

Test days 169-182: PR-AUC 0.5201, recall @ 1% FPR 0.4545, flagged 2.27%.

| File | SHA-256 |
|---|---|
| model.txt | `18cb0bd755427a8abbbb9cce55e3ba46d20b2bce5a5075d332f0bacff4109d71` |
| meta.json | `ff6994bb525227292be9fae88cae233a4ecff8456433cb1264b8154131f6891c` |

Compare with the live model: `python -m ml.eval.promotion_gate --candidate models/fraud-lgbm-v2 --current models/fraud-lgbm-v1` (judged on the test days above).
