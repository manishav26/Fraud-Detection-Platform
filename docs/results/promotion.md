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

## On Kubernetes

k3d cluster with 3 nodes and 2 scoring pods, deployed with `make k8s-up`.

| Check | Result |
|---|---|
| Promote version 2 during a 50/s load test | Both pods logged `now serving fraud-lgbm-v2`; 3,000 of 3,000 answered 200; every later answer said v2 |
| Delete a scoring pod | Its replacement answered v2 right away, not the pinned v1 |
| Delete Redpanda, then a scoring pod | Still v2: the promotion was kept on Redpanda's disk |
| Forged message | Both pods: `refused promotion message` |
| Oldest message left in the topic sent again | Both pods: `refused old promotion`, stayed on v1 |
| Version 3 signed, then its file changed, with scoring stopped | Both new pods: `refused model ... does not match its signed SHA-256`, then served v1, the last genuine promotion |

**ST9 passes on Kubernetes too**, on both pods.

Latency in that load test was p50 30.7 ms, p95 1,708 ms, p99 3,221 ms, with no errors. The cluster
shares the laptop's CPU with everything else, and the slow period isn't explained (see below).

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
- **Control runs, no promotion:**

  | Run | p50 ms | p95 ms | p99 ms |
  |---|---:|---:|---:|
  | Control 1 | 23.9 | 1,393.4 | 2,865.3 |
  | Control 2 | 14.4 | 35.4 | 76.1 |

- **Later runs with slow-request logging and `make stall-probe`, k3d off:**

  | Run | p50 ms | p95 ms | p99 ms |
  |---|---:|---:|---:|
  | No promotion, run 1 | 24.3 | 132.9 | 414.7 |
  | No promotion, run 2 | 24.4 | 76.8 | 332.0 |
  | No promotion, run 3 | 18.5 | 60.9 | 121.2 |
  | No promotion, run 4 | 15.0 | 39.2 | 64.6 |
  | Promotion during the run | 15.3 | 45.0 | 109.6 |

- **A promotion doesn't need to cause a stall:** the last run swapped with p95 45 ms. Big stalls
  did appear in the first 3 promotion runs and in 1 of the first 3 runs without one, so they aren't
  tied to promotion alone.
- **Big stalls are unexplained.** They lasted about 10 s and none has appeared since the tools were
  added, so the tools have not observed one. In the runs checked, Cassandra's logs showed no pauses
  during them, and no other service logged anything.
- **Smaller slowdowns:** most of the time was outside scoring's own steps, between ingest and
  scoring. The probe found almost no late timers on the laptop or in the VM.
- **For the baseline:** one run can mislead. Run it several times, and report each run. Slow-request
  lines and `make stall-probe` stay available, and per-service metrics (week 3, step 7) should show
  which hop slows down if it returns.
