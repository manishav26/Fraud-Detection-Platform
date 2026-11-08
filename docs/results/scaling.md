# Scale and resilience (week 4, step 2)

Design: `docs/k8s.md` ("Scale and resilience"). k3d cluster, 3 nodes on one laptop, 8 Nov 2026.
Each test: `make k8s-resilience TEST=...` (`scripts/resilience_test.sh`), real payments through Kong,
each test on its own rows. Raw output: `data/resilience/<test>/` (not in git).

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
1. **Kong reaches ingest's pods directly:** a headless Service, `ingest-pods`, whose DNS name
   returns each ready pod. Kong balances across them and, within its 5 s DNS refresh, stops
   choosing a pod that is leaving. Compose has the same name as an alias.
2. **Ingest waits 15 s before stopping** (was 5 s), longer than that refresh.
3. **Ingest retries a scoring call once if the connection breaks** (not after a timeout).
   Scoring the same payment twice gives the same answer and history row, so this is safe.
   Counted in `ingest_scoring_retries_total`.
4. **A crashed request is now counted as a 500** in `http_requests_total`.

With these, the same disruption (scoring, ingest and alert pods evicted from one node) gave
**0 errors in 5,400**.

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
