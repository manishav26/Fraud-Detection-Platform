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

## Results

*(to be filled in from the VM run)*

| Asked /s | Reached /s | p50 ms | p95 ms | p99 ms | ingest p95 ms | 5xx | degraded | scoring copies | Result |
|---|---|---|---|---|---|---|---|---|---|

Where the time goes (p95 per hop, ms):

| Asked /s | End to end | Kong total | Kong itself | Kong -> ingest | ingest server | publish txn.raw | wait for scoring | scoring /score | Cassandra read | model | reasons | Cassandra write | features fresh (p95 s) | feature job / sent |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
