# Gate 2: ML side

## 1. Goal
The ML part gives every payment a calibrated fraud probability, which the scoring service turns into approve, review or decline. Only about 3.5% of payments are fraud, so accuracy is misleading: a model that approves everything would be 96.5% accurate. I therefore measure PR-AUC and the share of fraud caught when only 1% of genuine payments are flagged.

## 2. Data and features
I use IEEE-CIS and split it by time (train days 1–73, validation 74–109, stream 110–182), so the model is always tested on payments that happen after the ones it learned from, as in real use.

The dataset has no customer ID, so I build a proxy user ID from card1, addr1 and the card's start day; per-user history features depend on it. The model uses 65 features: 51 raw columns, log amount and hour, and 12 history features such as payment counts in the last hour and day and how unusual the amount is for this user. Labels are released 14 days late, as in real systems.

## 3. Model choice (E1)
I compared logistic regression, LightGBM and XGBoost, each tree model with 3 random seeds. LightGBM and XGBoost were tied (the gap was smaller than the seed-to-seed spread), and both were far ahead of logistic regression (PR-AUC about 0.57 vs 0.22). I kept LightGBM because it was already packaged and served. Turning the D columns into fixed dates gave a small but consistent gain (+0.014 in all 6 runs); I deferred it to v2 because it needs a change in the live service.

## 4. Model v1
A 25-trial random search found a setting that was 0.017 better on the validation days, but it made the explanations take 68 ms per payment, far over the 10 ms budget. I limited the model size so explanations stay fast; the chosen model reaches a test PR-AUC of 0.497 and catches 43.2% of fraud at 1% false alarms (was 42.1%). Scores are calibrated with isotonic regression, and the review threshold (0.55) is the cheapest one that keeps analyst reviews within 2% of payments; declines start at 0.70. The model is registered in MLflow and promoted with a signed event, and the scoring service swapped to it live in 0.23 s.

## 5. Train/serve parity
Training computes history features over a whole table, while live scoring computes them for one payment from Cassandra. If the two disagree, the model silently gets worse. On 10,000 replayed payments both give identical values for all 12 features, with and without the backfilled history. Before passing, the test caught a real bug: for users who always pay the same amount, training produced a tiny fake spread and a zscore in the millions, while live scoring gave 0. I fixed it on both sides and added a test.

## 6. Explanations
For each flagged payment the model returns its top 3 reasons (TreeSHAP), for example "C1=12", so an analyst can see why it was flagged. This takes 2.9 ms, within the 10 ms budget, so it runs in the live path.

## 7. Drift
Comparing the stream days with training, several inputs drifted strongly; the biggest was the M1–M9 fields, whose missing share fell from 63–78% to 28–39%, which points to a change in data collection. The model's score distribution barely moved, yet the share of fraud caught fell from about 45% to 30% in weeks 4–6. A detector watching missed fraud caught this change, but only 14 days later because of the label delay. Watching inputs or scores alone is not enough.

## 8. Open issues and next steps
- The new threshold catches 643 instead of 715 frauds on the test window, with far fewer reviews; we may switch to a rule that uses the full 2% review budget.
- `model_version` does not change after a promotion (same name in both models); with Urmi.
- Week 4: E3 (static vs periodic vs drift-triggered retraining) and E5 (injected drift).
- Week 5: E6 (evasion attack and defenses).