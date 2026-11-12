# Failure tests (week 5, on the VM)

The plan: kill a scoring pod, a Cassandra node and Redpanda under load, measure the time to
recover, and compute availability = MTBF / (MTBF + MTTR) with a stated, assumed MTBF.

## Design

`make k8s-failure KIND=scoring|cassandra|redpanda ROUND=1` (`scripts/failure_test.sh`): synthetic
payments (`load.synthetic`, so no rows of the stream are used up) at 30/s for 4 minutes through
Kong; one minute in, one pod is deleted. Before starting, everything must be healthy (3 Cassandra
nodes Up/Normal, Redpanda ready, at least 2 scoring copies).

| Kind | Killed | Expected |
|---|---|---|
| scoring | one of the scoring pods | the other copy keeps answering; a few slow or retried answers while ingest's connections move |
| cassandra | `cassandra-1` (one of 3 copies) | nothing visible: LOCAL_QUORUM needs 2 of 3, and the driver routes around the missing node |
| redpanda | `redpanda-0`, the only broker | payments can't be saved, so ingest answers 503 until it's back: the one single point of failure left |

Two repair times per run (`load/failures.py`):
- **Back:** Kubernetes' view: the pod ready again (Cassandra: all 3 nodes Up/Normal; Redpanda: the
  topics answer).
- **What clients felt (the MTTR used for availability):** from the kill to the answer of the last
  payment that noticed it (no answer, an error, or scored without history). Payments that noticed
  something before the kill are counted apart.

**MTBF is assumed, not measured:** each part failing about once a month (30 days); six weeks can't
measure it. The table also shows availability if it were once a week. A payment needs all three
parts, so the platform's availability is the product of the three.

Each kind runs 3 times (`ROUND=1`, `2`, `3`); MTTR is the mean.

## Results

VM, 12 Nov: 9 runs, 7,200 synthetic payments each, a pod deleted 60 s in.

| Run | Killed | Payments | Noticed (errors + degraded + slow) | Back in (s) | Clients felt it for (s) |
|---|---|---|---|---|---|
| scoring-1 | a scoring pod | 7,200 | 0 | 7 | 0 |
| scoring-2 | a scoring pod | 7,200 | 0 | 7 | 0 |
| scoring-3 | a scoring pod | 7,200 | 0 | 12 | 0 |
| cassandra-1 | cassandra-1 | 7,200 | 0 | 75 | 0 |
| cassandra-2 | cassandra-1 | 7,200 | 0 | 77 | 0 |
| cassandra-3 | cassandra-1 | 7,200 | 0 | 75 | 0 |
| redpanda-1 | redpanda-0 | 7,200 | 200 (64 + 0 + 136) | 36 | 57.3 |
| redpanda-2 | redpanda-0 | 7,200 | 195 (62 + 0 + 133) | 37 | 53.3 |
| redpanda-3 | redpanda-0 | 7,200 | 234 (64 + 0 + 170) | 36 | 55.7 |

("Slow": answered, but after more than 1.5 s, the p99 target. No run had a problem before its kill.)

| Failure | Runs | MTTR, what clients felt (mean) | Availability, MTBF 30 days | If MTBF were 7 days |
|---|---|---|---|---|
| scoring pod | 3 | 0 s | 100% | 100% |
| Cassandra node | 3 | 0 s | 100% | 100% |
| Redpanda broker | 3 | 55.4 s | 99.998% | 99.991% |
| **the platform** (all three multiplied) | | | **99.998%** | **99.991%** |

- **A scoring pod or a Cassandra node can fail without any client noticing.** Not one payment of
  43,200 was refused, degraded or slow: the second scoring copy took the traffic while Kubernetes
  replaced the first (7-12 s), and 2 of 3 Cassandra copies kept LOCAL_QUORUM working for the 75 s
  the node took to rejoin. These are the redundancies built for exactly this.
- **Redpanda is the single point of failure.** With one broker, payments can't be saved while it
  restarts (36 s), so ingest refuses them: 62-64 payments per run were refused after the sender's
  retries (503), and 133-170 more were answered only after 1.5 s. Clients felt it for about 55 s,
  20 s longer than the pod took to be ready: payments that waited out the outage, and ingest
  reconnecting to the restarted broker, are cleared in that time (a per-second view would show
  how the 20 s split). Nothing was lost: a 503 means "not saved, retry", and the next try worked.
- **The fix is the same as for Cassandra: three brokers**, with each topic kept on 3 of them, so
  one can fail like a Cassandra node does. Future work: it needs a 3-broker Redpanda on the VM's
  three agents and topics recreated with 3 copies.

**Measurement note:** while Redpanda was down the sender fell 35-38 s behind schedule (its 64
in-flight slots filled with payments waiting out ingest's 5 s timeout and retries), so it sent
fewer payments during the outage than at other times. That changes how many were affected, not
how long: the "felt" time runs to the last slow answer, which came after the broker was back.

