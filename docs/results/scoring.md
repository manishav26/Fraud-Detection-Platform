# Real scoring (week 2, step 5)

Local Docker stack, model `fraud-lgbm-v1` (SHA-256 pinned in `deploy/scoring.env`).

## Model loads only with matching fingerprints (ST9)

| Check | Result |
|---|---|
| Normal start | `Application startup complete`, no stub warning; answers carry `model_version: fraud-lgbm-v1` |
| Last character of the `model.txt` pin changed | `model.txt does not match its pinned SHA-256; refusing to load`, startup failed, no traffic served |
| Pin restored | starts normally again |

## Replay: first 5,000 stream payments (days 110-111), `make replay SPEED=0 LIMIT=5000`

All 5,000 answered 200. 178 were really fraud (3.56%).

| Decision | Payments | Really fraud | Not fraud |
|---|---|---|---|
| approve | 4,921 | 116 | 4,805 |
| review | 42 | 26 | 16 |
| decline | 37 | 36 | 1 |

- **62 of 178 frauds (35%) flagged** (review or decline); declines are 36 of 37 correct.
- **Only 17 of 4,822 genuine payments flagged (0.35%)**: few false alarms for customers.
- Review 0.84% and decline 0.74% of payments, close to the model card (0.8% / 1.0%).
- Cassandra holds 4,474 rows = 4,473 known users' payments + 1 manual test. The other 527
  had an `NA` in the user id: scored as cold start and not stored, as designed.
- Speed 0 sends everything at once, so the latency here (p50 194 ms) is queueing, not a
  measure of normal latency. 116 payments/s through the whole stack with the real model.

## Load: `make load RPS=50 SECONDS=30`

| Scorer | p50 ms | p95 ms | p99 ms |
|---|---|---|---|
| Stub (step 4, run 2) | 8.6 | 12.8 | 18.7 |
| Real model + Cassandra history | 12.1 | 22.6 | 37.7 |

The real model plus one Cassandra read and one write add about 3.5 ms at the median. Synthetic
payments fill only a few attrs, so they look unusual to the model (21% review): fine for load,
not meaningful as fraud results.
