# Gate 2: ML side

## 1. Goal
The ML part gives every payment a calibrated fraud probability, which the scoring service turns into approve, review or decline. Only about 3.5% of payments are fraud, so accuracy is misleading: a model that approves everything would be 96.5% accurate. I therefore measure PR-AUC and the share of fraud caught when only 1% of genuine payments are flagged.

## 2. Data and features
I use IEEE-CIS and split it by time (train days 1–73, validation 74–109, stream 110–182), so the model is always tested on payments that happen after the ones it learned from, as in real use.

The dataset has no customer ID, so I build a proxy user ID from card1, addr1 and the card's start day; per-user history features depend on it. The model uses 65 features: 51 raw columns, log amount and hour, and 12 history features such as payment counts in the last hour and day and how unusual the amount is for this user. Labels are released 14 days late, as in real systems.

## 3. Model choice (E1)
I compared logistic regression, LightGBM and XGBoost, each tree model with 3 random seeds. LightGBM and XGBoost were tied (the gap was smaller than the seed-to-seed spread), and both were far ahead of logistic regression (PR-AUC about 0.57 vs 0.22). I kept LightGBM because it was already packaged and served. Turning the D columns into fixed dates gave a small but consistent gain (+0.014 in all 6 runs); I deferred it to v2 because it needs a change in the live service.

## 4. Model v1
A 25-trial random search found a setting that was 0.017 better on the validation days, but it made the explanations take 68 ms per payment, far over the 10 ms budget. I limited the model size so explanations stay fast; the chosen model reaches a test PR-AUC of 0.498 and catches 43.4% of fraud  at 1% false alarms (was 42.1%). Scores are calibrated with isotonic regression, and the review threshold (0.55) is the lowest one that keeps flagged payments (review + decline) within the 2% analyst budget; a cost rule ($2–$10 per review) gives the same 0.55. Declines start at 0.70. The model is registered in MLflow and promoted with a signed event, and the scoring service swapped to it live in 0.23 s.

## 5. Train/serve parity
Training computes history features over a whole table, while live scoring computes them for one payment from Cassandra. If the two disagree, the model silently gets worse. On 10,000 replayed payments both give identical values for all 12 features, with and without the backfilled history. Before passing, the test caught a real bug: for users who always pay the same amount, training produced a tiny fake spread and a zscore in the millions, while live scoring gave 0. I fixed it on both sides and added a test.

## 6. Explanations
For each flagged payment the model returns its top 3 reasons (TreeSHAP), for example "C1=12", so an analyst can see why it was flagged. This takes 2.9 ms, within the 10 ms budget, so it runs in the live path.

## 7. Drift
Comparing the stream days with training, several inputs drifted strongly; the biggest was the M1–M9 fields, whose missing share fell from 63–78% to 28–39%, which points to a change in data collection. The model's score distribution barely moved, yet the share of fraud caught fell from about 45% to 30% in weeks 4–6. A detector watching missed fraud caught this change, but only 14 days later because of the label delay. Watching inputs or scores alone is not enough.

## 8. Open issues and next steps
- Threshold: settled. We use the capacity rule (lowest threshold within the 2% review budget, chosen on days 74-91). It gives the same 0.55 as the cost rule, because calibrated scores move in steps and the next step down goes over 2% (0.55 flags 1.66% on validation days, 1.28% on test).
- `model_version`: settled. Urmi adds a short hash of `model.txt` to the label (e.g. `fraud-lgbm-v1-26eff82a`) in the pins PR.
- Reasons timing: 2.9 ms p95 measured on a laptop, one thread, outside a container, model call only. The cluster showed 24-49 ms p95 with the old week 2 model under load; to be measured again once tuned v1 is pinned.
- Week 4-5 done (offline): E3 retraining (+0.04 PR-AUC over static; drift-triggered ties periodic but retrains 4 vs 2 times), E5 injected drift (feature PSI catches input drift in 0-1 days; only ADWIN catches concept drift, 18 days incl. the 14-day label delay), E6 evasion (hiding the score + probe detection cut success at Q=50 from 50% to 17.5% with +0.001 points false positives; a per-card budget cost +0.49 points and added nothing), promotion gate (tuned v1 vs week-2 model: +0.001 PR-AUC, a tie; v1 kept for reproducibility and fewer flags).
- E6 live (Urmi, `docs/results/e6_live.md`): D1 and D3 are built and tested on the cluster, with 0 approvals after the 5th flag. But about 55% of probing attacks win before the 5th flag, vs 17.5% offline, because each try is saved to the card's history and the attacker's e-mail and device stop looking new. Kept as is; the fix (flagged payments don't make an e-mail or device familiar) is future work, since it needs a retrain.
- Next: MLflow training details (params, data window, metrics, git commit), final figures, then the report and slides.