# Baseline (week 3, step 8)

The first real latency and error numbers. Every later change (Spark, autoscaling, mTLS, failure
tests) is compared with these.

## How it was run

`make baseline ON=k8s RUN=1` (script: `scripts/baseline.sh`). Each run:

1. **Warm-up:** 1 minute at 30 payments per second, not counted.
2. **Run:** 10 minutes at 30 per second, 18,000 payments.
3. **Numbers:** the snapshot (`observability/snapshot.py`) for exactly that window, plus Cassandra's
   pauses. Output goes to `data/baseline/<where>-<run>/summary.md`.

| Choice | Why |
|---|---|
| Real payments (`load.replay --rps 30`), not synthetic | Real users have history in Cassandra, so reads and writes are tested. Synthetic users are unknown, and scoring skips Cassandra for them |
| 30 per second | The replay's average speed (`docs/load.md`) |
| Fresh payments every run (`--offset`) | Each run gets its own 20,000 rows. A repeated `txn_id` would put a user's later payments into their history |
| Our open-loop sender, not k6 or Locust (the plan named those) | It sends each payment at its scheduled time, like k6's fixed arrival rate, so a slow platform shows as latency instead of quietly slowing the test. Locust waits for each answer by default, which hides slow periods. It also uses real tokens through Kong |
| 3 warm runs per setup, plus 1 fresh run on the cluster | Spread between runs, and how slow the first minute after a deploy is |
| Cluster and compose | The plan asks for compose; the demo runs on the cluster |

Setup: k3d cluster (3 nodes on one laptop) with 2 copies each of ingest, scoring and alert-svc;
compose with 1 copy each. Model fraud-lgbm-v1, backfilled history.

Compose's Cassandra was **not backfilled**: it held only what earlier runs wrote (about 32,000
users, still in memory). So many more of its payments were cold starts (9,900–13,000 of 18,000,
against 6,800–7,800 on the cluster), and its history reads and writes were lighter. Compose is the
"same code, less work" comparison, not a like-for-like one.

Run on 5 Nov 2026, 12:45–14:37, one run after another, with nothing else heavy on the laptop.

## Results

### End to end (the sender, through Kong): what a merchant sees

18,000 payments per run, sent at exactly 30 per second.

| Run | p50 | p95 | p99 | Over 1 s | Errors | Degraded | Furthest behind |
|---|---|---|---|---|---|---|---|
| cluster 1 | 18 ms | 334 ms | 1,326 ms | 290 | 0 | 97 | 2.8 s |
| cluster 2 | 21 ms | 127 ms | 738 ms | 92 | 2 (one 500, one 502) | 4 | 2.8 s |
| cluster 3 | 22 ms | 103 ms | 653 ms | 107 | 2 (502) | 5 | 1.1 s |
| **cluster, mean (range)** | **20 ms** | **188 ms (103–334)** | **905 ms (653–1,326)** | 163 | 0.007% | 0.2% | |
| compose 1 | 16 ms | 53 ms | 387 ms | 48 | 0 | 2 | 1.1 s |
| compose 2 | 16 ms | 54 ms | 441 ms | 69 | 0 | 1 | 0.7 s |
| compose 3 | 14 ms | 25 ms | 63 ms | 7 | 0 | 0 | 0.3 s |
| **compose, mean (range)** | **15 ms** | **44 ms (25–54)** | **297 ms (63–441)** | 41 | 0 | 0.006% | |

"Degraded" means scored without history, because the Cassandra breaker was open or a read failed.

### Inside the platform (Prometheus)

| Run | ingest p95 | ingest p99 | scoring p95 | ingest waits for scoring (p95) | breaker open | Peak node CPU | Busiest pod |
|---|---|---|---|---|---|---|---|
| cluster 1 | 113 ms | 612 ms | 43 ms | 73 ms | about 5 s | 1.76 cores | cassandra-0, 0.53 |
| cluster 2 | 55 ms | 292 ms | 25 ms | 45 ms | 0 s | 0.97 | cassandra-0, 0.38 |
| cluster 3 | 58 ms | 372 ms | 26 ms | 47 ms | 0 s | 1.06 | cassandra-0, 0.40 |
| compose 1 | 41 ms | 322 ms | 21 ms | 27 ms | 0 s | – | – |
| compose 2 | 38 ms | 211 ms | 20 ms | 25 ms | 0 s | – | – |
| compose 3 | 23 ms | 44 ms | 10 ms | 20 ms | 0 s | – | – |

No 5xx from any service in any run. Cassandra logged no memory-cleanup pause (over 200 ms) during
any run. These server figures cover 11 minutes, so they also include about 1,750 warm-up payments
(see "Fixes to the tooling" below). The end-to-end figures cover the run only.

### What the numbers show

- **Slowness comes in bursts.** Per-minute p95 from each run's `run.csv`: most minutes are 20–100
  ms, and each run has one or two bad minutes (cluster 2's sixth minute: 816 ms; compose 1's
  seventh: 348 ms). The p99 is set by those bursts, not by steady slowness. Compose has them too,
  so it's not only the cluster. No Cassandra pause was logged at those times, so the cause is
  still open (candidates: Cassandra writing its memory to disk, Python's own memory cleanup, or
  Docker on the laptop). That is the first thing to look at in week 4.
- **The first run after the cluster starts is the slowest.** Cluster 1 came right after
  `k3d cluster start`. Its first minute after warm-up had a p95 of 1,776 ms, and its breaker opened
  for about 5 s (97 degraded). Runs 2 and 3 didn't. One minute of warm-up isn't enough after a
  cold start, so the demo should warm up for longer (see below).
- **The cluster adds about 50–70 ms at p95 outside the services.** On the cluster, the end-to-end
  p95 was roughly double ingest's own (127 vs 55 ms, 103 vs 58 ms). On compose the gap was 2–16
  ms. The extra time is in Kong, k3d's port mapping and Kubernetes' routing, and the time before
  ingest's event loop picks up the request. Ingest's own p95 timer doesn't count those.
- **The cluster had 5 errors that ingest never saw:** one 500 and four 502s, out of 72,000
  payments. They came from Kong, not from the services. A 502 usually means Kong's connection to
  ingest was closed under it. It's rare, but it needs explaining before the failure tests in
  week 5.
- **CPU is not the limit at 30/s.** The busiest node peaked at 1.0–1.8 cores, and Cassandra was
  the busiest pod in every run (0.38–0.53 cores). The 2.7 cores seen on 4 Nov didn't come back.
- **Scoring itself is quick.** p95 per step: history read 8–15 ms, write 5–10 ms, model 5 ms. The
  slowest step is reasons (24–49 ms): that's the place to save time if scoring needs to be faster.

## The first minute after a deploy (cluster, fresh run)

ingest and scoring were restarted, and the 18,000 payments started as soon as both were ready,
with no warm-up.

| | p95 | p99 |
|---|---|---|
| First minute, end to end | 1,530 ms | 6,464 ms |
| First minute, ingest (server) | 375 ms | 1,758 ms |
| First minute, ingest waits for scoring | 234 ms | – |
| First minute, scoring answers `/score` | 81 ms | 461 ms |
| Minutes 2–10, end to end | 59 ms | 457 ms |
| Whole run, end to end | 317 ms | 1,258 ms |

The sender fell 11 s behind schedule in the first minute, and one 502 came back. After that, the
run looked like a warm one (minute 3 had another burst). One slow minute is enough to triple the
whole run's p95, so a deploy just before the demo, or a rollout during it, will show.

The first minute is slow in both services, not in Cassandra: scoring's history read was 24 ms at
p95, against 8 ms later. Ingest waited for scoring about three times longer than scoring took to
answer. So the time is lost in getting each request to a new scoring pod (opening connections, the
first calls into the model library), not in the scoring work itself.

## What the numbers suggest for the targets

Suggestions for the Gate 2 joint check. The targets are for the cluster at 30/s, measured end to
end through Kong after warm-up:

| Measure | Baseline (cluster, mean of 3) | Suggested target |
|---|---|---|
| p95, end to end | 188 ms (103–334) | under 250 ms |
| p99, end to end | 905 ms (653–1,326) | under 1.5 s |
| ingest p95 (server) | 75 ms | under 100 ms |
| errors (5xx) | 0.007% | under 0.05% |
| degraded | 0.2% | under 1% |
| first minute after a deploy | p95 1.5 s | warm up for at least 3 minutes before the demo |

These leave room for the week 4–5 changes (Spark, mTLS, autoscaling), which all add work. If one
of them pushes the numbers past these, it shows. Any later change is compared with the mean and the
range above, never with one run. A difference smaller than the range between runs is noise.

## Fixes to the tooling

- **The server window was 11 minutes, not 10 (fixed after these runs).** `scripts/baseline.sh`
  rounded the run's length up to whole minutes. The replay takes a few seconds to load before
  sending, so 10 minutes became 11, and Prometheus's figures above include most of the warm-up.
  The warm-up is the same kind of traffic, so the figures are still usable. The script now passes
  the run's exact length in seconds (`snapshot --seconds`), so later runs cover the run only.
- **Compose should be backfilled before its baseline.** Rerun the compose runs after
  `make backfill`, so they are comparable with the cluster.
