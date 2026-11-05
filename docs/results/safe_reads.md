# Safe Cassandra reads (week 3, step 4)

k3d cluster, backfilled data. Design: `docs/scoring.md`.

## Read times, Cassandra idle (`make k8s-read-timing`, through a tunnel)

| Users | Payments per user | Reads | p50 ms | p95 ms | p99 ms | max ms | Over 50 / 100 / 200 ms |
|---|---|---:|---:|---:|---:|---:|---:|
| 100 random users | 1 to 13 | 500 | 7.2 | 11.7 | 15.3 | 19.5 | 0 / 0 / 0 |
| 20 largest users | 77 to 295 | 100 | 12.8 | 33.1 | 37.3 | 54.3 | 1 / 0 / 0 |

Heavy users aren't the problem: the largest known user in the backfilled data has 295 payments.

## First limits (reads 50 ms, writes 200 ms) were too tight

Replay of 2,000 payments, Cassandra healthy: all 200, but **290 degraded (14.5%)**, and 282 writes
skipped, leaving gaps in history. Both pods' breakers opened within a second of each other, about 10 s
into a slow period. In the slow-request log for that period:

| Step | Median ms | Max ms | Over 50 ms |
|---|---:|---:|---:|
| Read | 51 | 928 | 31 of 55 |
| Write | 120 | 1,127 | 45 of 55 |
| Predict | 4 | 98 | 3 of 55 |
| Reasons | 0 | 204 | 3 of 55 |

Limits changed to reads 150 ms, writes 500 ms. Predict normally takes under 1 ms and doesn't touch
Cassandra, so it being slow at all suggests the pods were short of CPU, not only Cassandra being slow.
That is a clue about the unexplained slow periods, not proof.

## Cassandra outage under load (first limits)

`make load RPS=50 SECONDS=60`, Cassandra stopped about 15 s in.

| | Result |
|---|---|
| Answers | 3,000 of 3,000 with 200 |
| Degraded | 2,094 |
| Latency | p50 20.6 ms, p95 58.3 ms, p99 146.9 ms |
| One payment during the outage | 32 to 76 ms, `degraded: true` |

## With the new limits (reads 150 ms, writes 500 ms)

The same first 5,000 stream payments, `make replay SPEED=1000 LIMIT=5000`, all answered 200.

| | Cassandra healthy | Cassandra stopped |
|---|---:|---:|
| Degraded | **8 (0.16%)** | 4,473 (every known user) |
| p50 / p95 / p99 ms | 31.6 / 636.1 / 1,396.7 | 13.0 / 441.4 / 1,108.1 |

The breaker didn't open in the healthy run (down from 290 degraded of 2,000). Those 8 were single
timeouts.

**The slow periods aren't only Cassandra:** p95 is still 441 ms with Cassandra not being called at
all, on a k3d cluster. Cassandra itself adds about 18 ms at p50.

## Recovery

Cassandra stopped, then started, with no restart of scoring:

| Load test, 300 payments at 20/s | Degraded | p50 / p95 / p99 ms |
|---|---:|---:|
| Cassandra stopped | 300 | 16.5 / 44.3 / 329.2 |
| Just after Cassandra returned | 62 | 19.9 / 53.7 / 191.2 |

After that, 6 of 6 test payments answered `degraded: false` with real history. **Recovery works.**

**Open: the breaker's log lines are unreliable.** In this run neither pod logged `Cassandra back`, and
one never logged `Cassandra failing`, although an earlier run logged both on both pods. The same code
logs all of them correctly against a fake store under parallel load, so the cause is unknown. Until
it is found, don't use those lines as evidence of breaker state or skipped-write totals. Step 7 should
expose breaker state and skipped writes as metrics.

## Do degraded payments get harsher decisions?

One test payment with almost no data scored 0.575 (review) with Cassandra up and 0.889 (decline)
during an outage. So we measured real payments (178 frauds, 4,822 genuine):

| | Cassandra healthy | Cassandra stopped |
|---|---:|---:|
| Approve / review / decline | 4,918 / 44 / 38 | 4,911 / 50 / 39 |
| Frauds flagged | 63 | 68 |
| False alarms | 19 | 21 |
| Genuine customers declined | 1 | 1 |
| Frauds declined | 37 | 38 |

- **Not harsher in practice:** no more genuine customers were declined, and false alarms rose by 2.
  21 of 5,000 decisions changed. The test payment was unusual because it had almost no fields.
- **Scores do rise:** 3,219 went up and 188 down, with the mean going from 0.030 to 0.038. Few
  crossed a threshold.
- **Decision: the model keeps deciding during an outage.** No special rule is needed.
- **Limits:** one sample of about 2 days and 178 frauds. One fraud that history caught was missed
  without it (0.913 to 0.370), which is the burst-type risk in `docs/scoring.md`.
