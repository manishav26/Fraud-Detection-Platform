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

*(to be filled in from the VM runs: `make k8s-failure KIND=report`)*
