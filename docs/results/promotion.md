# Signed promotion and live reload (week 3, step 2)

Local compose stack. Design: `docs/mlflow.md`.

## Promotion

| Check | Result |
|---|---|
| Promote version 2 during a 50/s load test | `model_version` became `fraud-lgbm-v2` with no restart; 3,000 of 3,000 answered 200 |
| Roll back: promote version 1 again | `fraud-lgbm-v1`, same score (0.5753), new sequence |

The versions hold identical files, so only the name changes. That proves the swap; a retuned model
would change scores too.

## Attacks (threat 11, ST9)

| Attack | Scoring's response | Model afterwards |
|---|---|---|
| Forged message in `model.events` | `refused promotion message: Not enough segments` | v1 |
| Old genuine message sent again (version 2) | `refused old promotion: sequence 1793655174200 is below the current 1793655237218` | v1 |
| Version 3 properly signed, then 1 byte added to its `model.txt` in storage | `refused model, keeping fraud-lgbm-v1: model.txt of version 3 does not match its signed SHA-256` | v1 |

In the last attack scoring was restarted, so it had to work out the live model from the topic. It
refused the tampered newest promotion and used the previous genuine one (v1 at sequence
1793655237218), not the older v2 or the tampered v3.

**ST9 passes:** scoring refused the swapped file and kept serving a trusted model.

## Open: latency during the promotion test

`make load RPS=50 SECONDS=60`, all 3,000 answered 200 in every run:

| Run | p50 ms | p95 ms | p99 ms | Time taken |
|---|---|---|---|---|
| k3d on, promotion during the run | 184.7 | 2,307.4 | 3,861.8 | 61.1 s |
| k3d off, no promotion | 15.0 | 35.9 | 53.0 | 60.0 s |
| k3d off, promotion during the run | 17.7 | 1,029.6 | 2,744.4 | 63.0 s |
| k3d off, promotion during the run (with timing log) | 19.1 | 1,091.1 | 2,613.6 | 60.0 s |

- **A running k3d cluster was the main cost:** with it off, p50 fell from 185 ms to about 15 ms.
- **The swap itself is short:** 0.24 s to download and 0.05 s to check and load. In the scoring log
  the rate stayed at 50 requests per second through the swap.
- **Still unexplained:** the slow requests in the last run came 27 to 38 s after the swap, when the
  rate fell to about 25 per second and then caught up. Cassandra, Redpanda, MLflow, ingest, Kong,
  Postgres and RustFS logged nothing at that time. So a link to the promotion isn't shown, and with
  one run lacking a promotion, neither is a link to the laptop.
