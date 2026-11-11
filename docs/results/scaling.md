# Scale and resilience (week 4, step 2)

Design: `docs/k8s.md` ("Scale and resilience"). k3d cluster, 3 nodes on one laptop, 8 Nov 2026.
Each test: `make k8s-resilience TEST=...` (`scripts/resilience_test.sh`), real payments through Kong,
each test on its own rows. Raw output: `data/resilience/<test>/` (not in git).

**Note (10 Nov):** these runs were made before Linkerd was added (also 8 Nov; the HPA's 2 to 3
copies below show it still worked then). From Linkerd until 10 Nov the
HPA couldn't scale at all: every scoring pod gained a `linkerd-proxy` container with no CPU
request, the HPA's pod-level CPU metric needs a request for every container, and it reported
`cpu: <unknown>` ("missing request for cpu in container linkerd-proxy"), keeping 2 copies. Found
while explaining a replay's degraded answers; fixed by scaling on the scoring container's own CPU
(`ContainerResource`) and giving every proxy requests (`docs/k8s.md`, `docs/mtls.md`). Week 5's
scaling test measures it again, on the VM.

## HPA after the fix (10 Nov, `make k8s-resilience TEST=scale`)

With the HPA on the scoring container's CPU and every proxy given requests:

| Check | Result |
|---|---|
| HPA status | `ScalingActive True` ("ValidMetricFound ... cpu container resource utilization"); `cpu: 1%/60%` at rest, where it had read `<unknown>` since Linkerd |
| proxy resources | 10m CPU, 20 MiB requested, 128 MiB limit on both scoring pods; under load the proxies used 6-14 MiB, 0 restarts |
| load | 80/s asked, **37.9/s reached** (28,800 in 759 s), every one answered 200 |
| scoring CPU (Prometheus, per pod, 1 min rate) | mostly 40-58% of the 500m request; single pods spiked to 62-68% for 1-1.5 min in all, never both at once |
| copies | **stayed at 2**: correct, since the HPA scales on the average across pods, which never reached 60% |

So the HPA now works and decides correctly; this laptop just can't send enough to need a third copy
(the gateway path tops out below scoring's capacity, as the runs below found too). An actual scale-up
is measured in week 5's scaling test on the VM.

Also seen in this run: 2 payments got the new 503 "recorded but not scored: retry" (scoring didn't
answer in 5 s at 02:17:46-47, the busiest minute) and both retries got 200; the scoring breakers
opened 12 and 14 times (Cassandra missing its 150 ms read limit while the laptop carried everything),
so 2,649 payments (9%) were scored without history; and 4 decisions failed to reach `txn.scored`
(`ingest_scored_publish_failures_total`), the accepted risk in `docs/ingest.md`.

## Summary

Final runs, with both fixes below in place:

| Test | Load | What happened to the pods | Errors (client) | p95 / p99 (client) |
|---|---|---|---|---|
| kill | 30/s, 3 min | scoring pod deleted at 60 s, ingest pod at 120 s | **0 of 5,400** | 86 / 873 ms |
| drain | 30/s, 3 min | a node's scoring, ingest and alert pods evicted (PDB: 1 of each kept) | **0 of 5,400** | 53 / 380 ms |

All runs, in order:

| Run | Fixes in place | Errors | p95 / p99 | Note |
|---|---|---|---|---|
| scale, 80/s asked, 58/s reached, 8 min | none | 14 of 28,800 | 1,089 / 1,901 ms | HPA: 2 -> 3 copies; bursts at 162 s and 358 s |
| kill | none | 0 of 5,400 | 323 / 1,541 ms | |
| rollout | none | 0 of 5,400 | 73 / 229 ms | every scoring pod replaced in 34 s |
| drain | none | 9 of 5,400 | 576 / 1,425 ms | all at 18-19 s, before the drain: a keep-alive burst |
| kill, rerun 1 (2 min) | keep-alive | 0 of 3,600 | 1,148 / 2,091 ms | |
| drain, rerun 1 (2 min) | keep-alive | 16 of 3,600 | 1,313 / 5,818 ms | all at the second an **ingest** pod was evicted |
| kill, rerun 2 | keep-alive + pods directly | **0 of 5,400** | 86 / 873 ms | |
| drain, rerun 2 | keep-alive + pods directly | **0 of 5,400** | 53 / 380 ms | same three pods evicted as rerun 1 |

**Done-when from the plan:**
- "Under load replicas rise above 2": met. The HPA asked for a third copy 43 s after the load
  started (CPU at 75% of target), and it was ready 21 s later.
- "Killing a pod causes no visible error spike": met after two fixes. Deleting a scoring pod and
  an ingest pod, a rolling restart, and a node drain under load gave 0 errors. Before the fixes,
  two separate problems caused short bursts of 502/500 (below).

One run of each final test, so the result is "no errors seen in 10,800 payments across both
disruptions", not a rate. The scale test was not repeated with the fixes.

## Scale: what limited it

The sender asked for 80/s but reached 58/s, falling 134 s behind. Inside the platform, everything
was healthy at 57/s (Prometheus, the test's window):

| | p95 |
|---|---|
| ingest (its own answer time) | 89 ms |
| scoring (queue + work) | 39 ms |
| Kong, total (of which Kong itself) | 137 ms (23 ms) |
| **the client, through Kong** | **1,089 ms** |

- **Scoring was not the limit**, so the HPA correctly stopped at 3 copies: CPU per copy settled at
  44-54% of the request, under the 60% target.
- **The busiest pod was Cassandra** (1.10 cores; the only copy). Its breaker opened for about 5 s,
  and 79 payments (0.3%) were scored without history.
- **About 950 ms of the client's p95 was outside the cluster:** between the sender and Kong.
  That path is Docker Desktop's port forwarding into k3d's load balancer, plus the Python sender
  itself, all on the same laptop CPU (the three nodes peaked at 2.0-2.2 cores each).
- So **this laptop's ceiling is about 58/s through the gateway**, set by the laptop and the path
  into the cluster, not by scoring. Week 5's ramp test should send from inside the cluster (a
  Job), or from another machine, to measure the platform without that path.

## Error cause 1: a keep-alive mismatch, made visible by laptop stalls

The 23 errors in the first runs (and the 5 in the baseline) have the same signature:
- They come in bursts of about 10 within one or two seconds.
- ingest's own error count showed none; Kong recorded every 500 and 502. (That count turned out to
  miss requests that crashed with an exception; fixed, see cause 2.)
- Kong's log says the ingest pod closed the connection while Kong was waiting for the answer:
  `upstream prematurely closed connection` and `Connection reset by peer`.
- At 10:32:10, the drain run's burst, **every pod on all three nodes failed its probes at the
  same moment** (MLflow, ingest, scoring, alert-svc, model-admin, Prometheus): the whole laptop
  stalled for over a second. No pod restarted.

The cause is a known mismatch between Kong and uvicorn:
- **uvicorn closes a keep-alive connection after 5 s idle.**
- **Kong keeps idle upstream connections for 60 s** and reuses them.
- So Kong can send a payment on a connection uvicorn is closing at that moment, and the payment
  fails. A stall makes many connections reach their 5 s at once, hence the bursts.

**Fix:** our services keep connections for 75 s (`--timeout-keep-alive 75`), longer than Kong's
60 s (now set explicitly), so Kong always closes them first. `tests/test_k8s_manifests.py` checks
the order.

The stall itself (Docker's VM pausing under load) is the laptop, not the platform. It is also
the most likely cause of the "slow bursts" in the baseline.

## Error cause 2: reused connections stay tied to a pod that is leaving

With the keep-alive fix, the kill rerun was clean, but the drain rerun had 16 errors, all in the
second an ingest pod was evicted (Kong's log: `Connection reset by peer`, `upstream prematurely
closed connection`). The first drain run never evicted an ingest pod, so it hadn't shown this.

- Kong sent to `ingest-svc`, one Service address. Kubernetes picks a pod when a connection
  opens, and Kong reuses that connection for many payments. So Kong's connections stayed tied to
  the old pod after Kubernetes removed it from the Service; the 5 s pause before stopping didn't
  help, since Kong kept using them through it. When the pod shut down and closed them, payments
  on them failed.
- The same happens between ingest and scoring.
- While reading this, a second gap showed: a request that crashed with an exception wasn't
  counted in the service's own metrics, so a 500 the client saw could be missing from ingest's
  error rate.

**Fixes:**
1. **Kong reaches ingest's pods directly** (replaced on 11 Nov: cause 3 below): a headless Service, `ingest-pods`, whose DNS name
   returns each ready pod. Kong balances across them and, within its 5 s DNS refresh, stops
   choosing a pod that is leaving. Compose has the same name as an alias.
2. **Ingest waits 15 s before stopping** (was 5 s), longer than that refresh.
3. **Ingest retries a scoring call once if the connection breaks** (not after a timeout).
   Scoring the same payment twice gives the same answer and history row, so this is safe.
   Counted in `ingest_scoring_retries_total`.
4. **A crashed request is now counted as a 500** in `http_requests_total`.

With these, the same disruption (scoring, ingest and alert pods evicted from one node) gave
**0 errors in 5,400**.

## Error cause 3: Kong kept expired addresses for ingest (VM, 11 Nov)

Fix 1 above was made before the mesh (Linkerd) existed, and stopped working with it.

**Seen:** the first replay after each `make k8s-reset-data` (which replaces both ingest pods) got
exactly 2 answers of 502, both among the first payments, answered in 7 and 13 ms; the long runs
afterwards had none. Kong logged no error. The Linkerd proxy beside Kong logged the cause:
`endpoint 10.42.0.13:8000 ... Connection refused` (and `10.42.1.18` after the second reset): the
addresses of the ingest pods the reset had removed, not the running ones (`10.42.0.27`,
`10.42.4.17`).

**Why:** with `ingest-pods` headless, Kong looks the pods up in DNS itself and, as far as we
can tell from its documented defaults, keeps serving an expired answer while it refreshes it in
the background (`dns_stale_ttl`), so the "5 s" above didn't hold: minutes after the reset it
still sent to the removed pods. And because Kong picked the pod, its Linkerd proxy only forwarded
to that address (`forward{addr=...}` in the log) instead of balancing.

**Fix:** Kong sends to a normal Service, `ingest-svc`, and its Linkerd proxy balances each
request across the ready ingest pods, following Kubernetes' list as it changes. Cause 2 doesn't
come back: the proxy balances requests, not connections, and drops a pod as soon as it leaves the
ready list; ingest's 15 s pause covers the requests already in flight. Ingest to scoring already
worked this way.

**Checked:** *(after applying: the reset and replay again, expecting 0 of 2,000 502s; the drain
rerun, the test that found cause 2, expecting 0 errors; ST2 and ST4)*

## Other observations

- **kill:** the p99 of 1,541 ms is the moment each pod went away: requests already on their way
  to it were retried by Kong or waited for the other copy. No errors.
- **rollout** was the cleanest run (p95 73 ms): new pods are ready before old ones stop, and the
  5 s pause lets old ones finish their requests.
- **drain:** the PodDisruptionBudgets let Kubernetes evict one scoring pod and one alert-svc pod
  from the node, keeping the other copy of each running, and the replacements started on the
  other nodes. The data stores weren't touched (`--pod-selector`): they have one copy with a
  node-local disk, which is week 5's failure-test subject.
- **The Grafana copies panel** showed scoring dropping to 1 for a moment under 80/s, while
  Kubernetes had 3 ready the whole time: a busy pod answered Prometheus's read late. The panel
  now counts a pod that answered any read in the last 30 s.

## Week 5: 1, 2 and 4 copies (on the VM)

The plan: run 1, 2 and 4 scoring copies at the same load and compute the scaling efficiency
E_N = T_N / (N x T_1) (Chapter 1).

**Design** (`make k8s-scaling`, `scripts/scaling_test.sh`, `load/scaling.py`): scoring held at
exactly N copies (HPA min = max = N, put back afterwards); each run sends the same 18,000 real
payments (rows 110,000-163,999, a new block per run) as fast as the senders' in-flight limit
allows, so the platform sets the pace (a closed loop). **Four sending processes** (each every
4th row, 64 in flight each, `load.replay --shard`): one Python sender tops out near 150/s (the
first VM ramp, `docs/results/ramp.md`), below what 2 or 4 copies may answer, which would make
the efficiency measure the sender instead. T_N is the payments answered per second between the 10th
and 90th percentile of answer times (no warm-up, no drain). Each run also records scoring's and
Cassandra's p95 and the busiest pods: if T_4 stops growing while the scoring pods have CPU to
spare, the limit is elsewhere (ingest's 2 copies, Kong, Cassandra or the sender), and the table
shows which.

### First run: 8-core VM, ingest at its usual 2 copies (11 Nov)

| Scoring copies | Throughput T_N (/s) | E_N | p50 ms | p95 ms | 5xx | scoring /score p95 ms | Cassandra read p95 ms | busiest pods (cores) |
|---|---|---|---|---|---|---|---|---|
| 1 | 252.8 | 1.00 | 886 | 1,865 | 0 of 18,000 | 797 | 84 | feature-job 1.32, ingest 0.89 + 0.88, scoring 0.80 |
| 2 | 244.6 | 0.48 | 829 | 2,346 | 0 of 18,000 | 262 | 90 | feature-job 1.21, ingest 0.88 + 0.88, cassandra-0 0.67 |
| 4 | 231.2 | 0.23 | 610 | 2,439 | 0 of 18,000 | 192 | 79 | feature-job 1.16, ingest 0.83 + 0.82, cassandra-1 0.65 |

(p50 and p95 here are of a closed loop with 256 payments in flight: they show queueing, not the
latency at a given rate, which is the ramp's job.)

**Throughput stayed at about 250/s whatever the number of scoring copies**, so E_N here measures
the bottleneck, not scoring. Scoring itself got faster with each copy (its p95 797 -> 262 -> 192
ms): with 1 copy it was a queue, with 2 or 4 it wasn't, and the platform still answered 250/s.
Two things were full instead:
- **ingest**: its 2 fixed copies at about 0.88 cores each, near one Python process's ceiling
  (it doesn't autoscale; at 200/s in the ramp it was the hop closest to its target);
- **the VM**: the k3d nodes' CPU peaks add up to 6.6-6.8 of its 8 cores, before the 4 sending
  processes, which run on the VM outside the cluster and aren't counted there.

So on 8 cores the platform tops out near 250 payments a second, limited by CPU (ingest and the
feature job most), not by scoring.

### Rerun: 16-core VM, ingest held at 4 copies

To measure scoring's own scaling, the rerun removes both limits: the VM is resized to
m7i.4xlarge (16 vCPU; `docs/vm.md`, "Resizing"), ingest is held at 4 copies for the whole test
(`INGEST`, put back afterwards), and the whole VM's CPU is logged every 2 s from `/proc/stat`
(the cluster and the senders together), shown per run as mean / peak busy cores.

*(results to be filled in from the rerun)*
