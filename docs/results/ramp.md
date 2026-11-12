# Ramp test (week 5, on the VM)

The plan: raise the request rate in steps until p95 passes the target, break p95 down by hop and
compare it with the latency budget. Also: how fresh the feature job's output stays as load grows.
On the measurement VM (`docs/vm.md`): 8 vCPU, 3-node Cassandra, the platform as deployed.

## Design

**Targets** (the Gate 2 targets, `docs/results/baseline.md`; through Kong, after warm-up):

| Measure | Target |
|---|---|
| p95 end to end | under 250 ms |
| p99 end to end | under 1.5 s |
| ingest p95 (server) | under 100 ms |
| errors (5xx, or no answer) | under 0.05% |
| degraded answers | under 1% |
| model: score / score + reasons | 5 ms / 10 ms (Manisha's budgets) |

Kong and Cassandra have no budget of their own: this test measures their share.

**Steps:** 25, 50, 75, 100, 150 and 200 payments a second, 3 minutes each, through Kong
(`load.replay --rps`, real payments in time order). Each step is measured over its **last 2
minutes**, so the first absorbs the change of rate and any scale-up. The ramp **stops after the
first step that misses any target**; that step and the one before it bracket the maximum. If 200/s
passes, the result is "at least 200/s": higher steps would need more rows than this test has.

**The platform as deployed:** the HPA stays on (2 to 8 scoring copies), and the copies ready are
recorded every 5 s. The scaling test holds them fixed (1, 2, 4).

**Is the sender the limit?** On the laptop the sender and the path into the cluster topped out
around 58/s before the platform did (`docs/results/scaling.md`). Here the sender keeps 256
payments in flight, and each step records the rate it reached in the measured window: a step under
95% of the asked rate is **sender-limited**, not a platform result, and stops the ramp. The hop
table shows it too: if the end-to-end p95 grows while Kong's stays flat, the time is outside the
platform.

**Hops** (p95 over the measured window, from metrics the services already export):

| Hop | Metric |
|---|---|
| end to end | the replay's own timings (`sent_s` in its CSV picks the window) |
| Kong total / Kong itself / Kong waiting for ingest | `kong_request_latency_ms`, `kong_kong_latency_ms`, `kong_upstream_latency_ms` |
| ingest server; its steps (publish to `txn.raw`, wait for scoring) | `http_request_duration_seconds{service="ingest-svc"}`, `ingest_step_seconds` |
| scoring `/score`; its steps (Cassandra read, model, reasons, Cassandra write) | `http_request_duration_seconds{service="scoring-svc"}`, `scoring_step_seconds` |
| feature job: `txn.raw` -> features written; payments it handled | `feature_job_delay_seconds`, `feature_job_payments_total` |
| busiest pods | CPU, as in `observability.snapshot` |

p95s don't add up: each hop's p95 is its own slowest 5%. Read the table as where the slow
requests spend their time. Freshness has no target yet (Spark's micro-batches take about 1-2 s);
scoring doesn't use these features (it reads the history rows directly, `docs/spark.md`), so
freshness affects analysts' data, not decisions.

**Run** (on the VM, after the check replay): `make k8s-ramp` (`scripts/ramp_test.sh`; about 20
minutes if every step runs). `scripts/ramp_test.sh plan` prints each step's rows. Rows of
`stream.parquet`: **2,000-109,999** (the check replay used 0-1,999; scaling uses
110,000-167,239; the consistency test 167,240 onward). Before the first step it checks that
Prometheus has every metric a step needs. Output: `data/ramp/<rate>/` and
`data/ramp/summary.md` (not in git).

## First run (11 Nov, one sender, before the tombstone fix)

| Asked /s | Reached /s | p50 ms | p95 ms | p99 ms | ingest p95 ms | 5xx | degraded | scoring copies | Result |
|---|---|---|---|---|---|---|---|---|---|
| 25 | 25.0 | 15 | 20 | 26 | 24 | 0.000% | 0.00% | 2 | pass |
| 50 | 50.0 | 13 | 21 | 28 | 23 | 0.000% | 0.00% | 2 | pass |
| 75 | 75.0 | 12 | 22 | 31 | 22 | 0.000% | 0.00% | 2 | pass |
| 100 | 100.0 | 13 | 27 | 42 | 24 | 0.000% | 0.00% | 2 | pass |
| 150 | 150.0 | 16 | 40 | 63 | 35 | 0.000% | 0.00% | 2 | pass |
| 200 | 148.6 | 926 | 4,154 | 6,353 | 67 | 0.000% | 0.00% | 2 | sender-limited |

Every target held up to 150/s. At 200/s the sender reached only 148.6/s, and its p95 of 4.2 s
was not the platform's: **Kong's own p95 for the same window was 91 ms** (ingest 67 ms). The time
was spent before requests reached Kong, in the one Python sending process (or k3d's load
balancer). So the run was repeated with **two senders** (`make k8s-ramp SENDERS=2`: each process
takes every other row at half the rate, `load.replay --shard`, so together they still send the
stream in time order), after the data was reloaded without tombstones. Also seen at 200/s: Kong's
own time rose from 1 to 23 ms, so Kong (one copy) is close to its limit too. The HPA kept 2 scoring
copies throughout: their average CPU never reached the 60% target.

## Second run (11 Nov, two senders, the old Kong path)

| Asked /s | Reached /s | p50 ms | p95 ms | p99 ms | ingest p95 ms | 5xx | degraded | scoring copies | Result |
|---|---|---|---|---|---|---|---|---|---|
| 25 | 25.0 | 16 | 23 | 29 | 24 | 0.000% | 0.00% | 2 | pass |
| 50 | 50.0 | 16 | 25 | 31 | 24 | 0.000% | 0.00% | 2 | pass |
| 75 | 75.0 | 15 | 26 | 35 | 24 | 0.000% | 0.00% | 2 | pass |
| 100 | 100.0 | 16 | 34 | 50 | 25 | 0.000% | 0.00% | 2 | pass |
| 150 | 150.0 | 20 | 47 | 72 | 42 | 0.000% | 0.00% | 2 | pass |
| 200 | 200.0 | 33 | 119 | 211 | 95 | 0.000% | 0.00% | 3 | pass |

Where the time goes (p95 per hop, ms):

| Asked /s | End to end | Kong total | Kong itself | Kong -> ingest | ingest server | publish txn.raw | wait for scoring | scoring /score | Cassandra read | model | reasons | Cassandra write | features fresh (p95 s) | feature job / sent |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 25 | 23 | 24 | 1 | 24 | 24 | 5 | 23 | 10 | 5 | 5 | 19 | 5 | 2.7 | 1.00 |
| 50 | 25 | 24 | 1 | 24 | 24 | 5 | 23 | 13 | 5 | 5 | 22 | 5 | 1.9 | 1.00 |
| 75 | 26 | 24 | 2 | 24 | 24 | 5 | 22 | 12 | 5 | 5 | 21 | 5 | 1.9 | 1.00 |
| 100 | 34 | 37 | 2 | 33 | 25 | 5 | 23 | 18 | 7 | 5 | 22 | 5 | 1.9 | 1.00 |
| 150 | 47 | 47 | 2 | 46 | 42 | 5 | 31 | 23 | 10 | 5 | 24 | 8 | 1.9 | 1.00 |
| 200 | 119 | 134 | 2 | 127 | 95 | 23 | 68 | 43 | 23 | 5 | 31 | 18 | 2.9 | 1.00 |

**Every target held up to 200/s**, the highest step this test's rows allow: the platform sustains
**at least 200 payments a second** (about 6.7 times the 30/s the targets were set for), p95 119 ms
against 250 ms.

- **The next limit is ingest**, at 95 ms against its 100 ms target at 200/s: it waits for scoring
  (68 ms) and for Redpanda to confirm the payment is saved (23 ms, up from 5). Kong itself stays
  at 2 ms, so the gateway is not the limit; Kong's total is ingest's time plus the hop to it.
- **Scoring scaled itself:** the HPA added a third copy during the 200/s step.
- **The model is flat at 5 ms** at every rate; what grows is waiting (Cassandra read 5 -> 23 ms,
  write 5 -> 18 ms, reasons 19 -> 31 ms as the scoring pods' CPU fills).
- **The feature job kept up at every rate** (it handled 1.00 of what was sent), with features
  about 2-3 s fresh at p95: Spark's micro-batches, not falling behind.
- **The one-sender run** (above) put its 4 s in the sender: with two, Kong's own time stayed at
  2 ms instead of 23.

**Caveat:** this run's scoring image still wrote missing values as nulls (the fix reached the
VM's code but not the running image), so its data had about 1 tombstone per history read
(`nodetool tablestats`: average 1.1, max 10). Cassandra's read p95 stayed at 5-23 ms, so this
can't have changed the result much. Scoring and the feature job are rebuilt with the fix before the remaining tests.

## Final run (11 Nov, two senders, the final setup)

After the tombstone fix reached the images and Kong was moved to the mesh-balanced `ingest-svc`
(`docs/results/scaling.md`, "Error cause 3"), on freshly reloaded data:

| Asked /s | Reached /s | p50 ms | p95 ms | p99 ms | ingest p95 ms | 5xx | degraded | scoring copies | Result |
|---|---|---|---|---|---|---|---|---|---|
| 25 | 25.0 | 18 | 24 | 29 | 24 | 0.000% | 0.00% | 2 | pass |
| 50 | 50.0 | 20 | 30 | 38 | 24 | 0.000% | 0.00% | 2 | pass |
| 75 | 75.0 | 19 | 34 | 43 | 24 | 0.000% | 0.00% | 2 | pass |
| 100 | 100.0 | 22 | 49 | 70 | 41 | 0.000% | 0.00% | 2 | pass |
| 150 | 150.0 | 34 | 97 | 170 | 69 | 0.000% | 0.00% | 3 | pass |
| 200 | 140.9 | 1,125 | 8,215 | 12,366 | 922 | 0.568% | 2.12% | 3 | not reached; targets missed |

| Asked /s | End to end | Kong total | Kong itself | Kong -> ingest | ingest server | publish txn.raw | wait for scoring | scoring /score | Cassandra read | model | reasons | Cassandra write | features fresh (p95 s) | feature job / sent |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 25 | 24 | 24 | 1 | 24 | 24 | 5 | 23 | 10 | 5 | 5 | 12 | 5 | 2.7 | 1.00 |
| 50 | 30 | 27 | 1 | 25 | 24 | 5 | 24 | 19 | 7 | 5 | 22 | 5 | 2.0 | 1.00 |
| 75 | 34 | 32 | 2 | 27 | 24 | 5 | 24 | 18 | 6 | 5 | 22 | 5 | 1.9 | 1.00 |
| 100 | 49 | 47 | 3 | 45 | 41 | 5 | 26 | 23 | 9 | 5 | 23 | 7 | 1.9 | 1.00 |
| 150 | 97 | 86 | 4 | 82 | 69 | 17 | 50 | 37 | 20 | 5 | 32 | 14 | 2.5 | 1.01 |
| 200 | 8,215 | 1,096 | 62 | 1,061 | 922 | 202 | 739 | 181 | 70 | 7 | 46 | 48 | 7.7 | 0.99 |

**On the final setup every target holds up to 150 payments a second** (p95 97 ms against 250),
five times the 30/s the targets were set for. At 200/s the 8-core VM is full: every hop slows at
once (Kong itself 62 ms, Redpanda 202 ms, ingest 922 ms), 0.57% of payments fail and 2.1% are
scored without history.

**Open: why this run is slower than the second one**, which held 200/s. Everything slowed
together, which points at less free CPU rather than one part: at 150/s the k3d nodes used about
6.5 cores against 5.3 in the first run (the second run's files were overwritten). Nothing was
left running on the VM outside the cluster. The runs differ in several ways at once (the Kong
path, the images, what ran before, and the HPA reaching 3 scoring copies at 150/s this time), so
the cause isn't isolated; an A/B run with only the Kong path switched would settle it.
