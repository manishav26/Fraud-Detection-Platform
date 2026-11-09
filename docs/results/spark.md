# Streaming feature job: results (week 4, step 1)

Design: `docs/spark.md`. k3d cluster on the laptop, 9 Nov 2026. Spark 4.2.1 in local mode
(`local[2]`), one pod (1 GiB requested, 2 GiB limit), with Linkerd, Redpanda and Cassandra as in
every other run.

## Parity: the job's features vs training's code

`make k8s-spark-parity`: the first 10,000 stream payments written to a fresh topic, the job
started on it with empty state, then every payment's 12 features read back from
`features_by_txn` and compared with `add_history` (the code training uses) over the same payments.

| Run | Payments | Equal (all 12 features) | Differ | Missing |
|---|---|---|---|---|
| Spark on the cluster | 10,000 | **10,000 (100.000%)** | 0 | 0 |
| Spark in its image, no cluster (`make spark-smoke`, 5 micro-batches) | 10,000 | 10,000 (100.000%) | 0 | 0 |
| Spark's per-user function and the Python writer, offline | 10,000 | 10,000 (100.000%) | 0 | 0 |

**Target 99.9%: passed with no mismatch.** The same-second payments that the live parity check
of week 3 had to exclude match here too: the job keeps a user's same-second payments in arrival
order, exactly as `add_history` counts them. 1,239 of the 10,000 come from unknown cards and get
missing features, as in training.

## Throughput

`make k8s-spark-throughput` (and `JOB=python`): 100,000 stream payments written to a fresh topic
first, then the job started with empty state. Payments per second from the end of its first batch
(which includes start-up) to the last.

| Job | Payments measured | Seconds | **Payments per second** | Batch |
|---|---|---|---|---|
| Spark (Structured Streaming, `applyInPandasWithState`) | 80,003 | 558.5 | **143** | up to 20,000 |
| Python writer (same state code, same writes) | 95,000 | 117.0 | **812** | up to 5,000 |

Both write the same rows to Cassandra (100,000 to `features_by_txn`, one per known user to
`user_features`), so the difference is the engine.

**Where Spark's time goes.** The Python writer's own feature maths takes well under a second per
batch (offline: about 23,000 payments per second), so its 812 per second is almost all Cassandra
writes: about 1.2 ms per payment, or about 25 s per 20,000. A Spark batch of 20,000 took about
130 s, so **about 100 s of each batch is Spark's own overhead**. Most of it is the per-user step:
Spark calls the Python function once for every user in the batch (thousands per batch), and
moves each user's rows and state between the JVM and Python (Arrow) for each call. This is the
known cost of `applyInPandasWithState` with many small groups.

**Is 143 per second enough?** For this platform, yes: the gateway path tops out at about 58
payments per second on this laptop (`docs/results/scaling.md`), and the steady test load is 30 per
second, so Spark keeps up with live traffic with 2.5-5x headroom. It is not enough to reprocess
history quickly: the 590,000-payment dataset would take about 70 minutes (the Python writer: 12).

**Ways to make it faster** (not done): fewer, larger groups per call (for example, grouping by a
hash bucket of the user and keeping a dictionary of users' states per bucket), a compiled
per-user step instead of pandas, more cores (`local[4]`), or a real Spark cluster with executors
across nodes, which this laptop can't hold.

## What the runs found and changed

| Finding | Change |
|---|---|
| After the laptop briefly overloaded during the first deploy, a node went NotReady for a moment; Kubernetes' list of Cassandra's addresses stayed marked "not ready" afterwards, and Linkerd refused new connections to it, so the restarting job couldn't connect | rebuilt the list (`kubectl delete endpointslice ...`); the job now waits up to a minute for Cassandra at start instead of crashing |
| One 1 s Cassandra memory pause made a whole batch of 10,000 writes time out; the Spark query stopped and restarted from its checkpoint (correct, nothing lost or double counted, but a minute of start-up) | only the failed writes are retried, with waits of 1, 2, 4, 8 and 15 s; 32 writes at a time instead of 64 |
| The Python writer asked Kafka for the topic's partitions before fetching the topic's details and got none | it subscribes to the topic instead |
| Cassandra 4.1 refuses a second password change for the same role within 5 s, which "create, then always change" ran into | `create_schema.sh` creates a login with its password in one statement and changes it only if it doesn't already work |
| Ctrl+C on a run left the job pointed at the test topic | the run script turns Ctrl+C into a normal exit, so its clean-up always runs |
| PySpark 4.2.1 ships Java libraries with 62 HIGH/CRITICAL findings | fixed releases swapped in at build time; 23 left inside Hadoop's and Spark's own jars, waived per jar (`docs/spark.md`, "Supply chain") |

## Not measured yet

- **Freshness under live load** (`feature_job_delay_seconds`, Grafana "Feature job: how fresh the
  features are"): seconds from a payment entering `txn.raw` to its features being written, during
  a normal 30-per-second replay. With one micro-batch a second, expected around 1-3 s. Week 5,
  with the ramp test.
