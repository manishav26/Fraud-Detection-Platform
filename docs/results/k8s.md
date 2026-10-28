# Kubernetes results (week 2, step 6)

k3d cluster `fraud` on one MacBook: 1 server + 2 agent nodes. `make cluster` then `make k8s-up`.
Kong 1, ingest-svc 2, scoring-svc 2, token-issuer 1, Redpanda 1, Cassandra 1.

## Deploy

All pods `Running 1/1`, placed by Kubernetes across all 3 nodes. scoring-svc restarted once at
first start (Cassandra not ready yet), then came up by itself.

Fixed on the way (details in docs/k8s.md): image import failing on Docker Desktop's
multi-platform images (`ErrImageNeverPull`), and Kong `OOMKilled` with 13 nginx processes.

## Same behaviour as compose

| Check | Result |
|---|---|
| Payment with token | 200, `fraud-lgbm-v1`, score 0.5753 `review`: identical to compose |
| No token | 401 from Kong |
| Replay, first 5,000 stream payments | all 200; approve 4,921 / review 42 / decline 37, **identical to compose** |

## Self-healing

`make load RPS=50 SECONDS=60`, and halfway through one of the two scoring pods was deleted.

| | Result |
|---|---|
| Payments answered | **3,000 of 3,000 with 200, no retries** |
| Replacement pod | created and `Running 1/1` within 11 s |
| Latency | p50 13.3 ms, p95 38.3 ms, p99 87.6 ms (compose without a pod kill: 12.1 / 22.6 / 37.7) |

The Service sent traffic to the surviving pod while the new one started, so clients saw no
errors; the kill shows up only as a slower worst 1%.

## Capacity

| Setup (real model) | Most payments/s |
|---|---|
| compose, 1 ingest + 1 scoring (replay at full speed) | about 116 |
| k8s, 2 ingest + 2 scoring (replay at full speed) | about 93 |
| k8s, synthetic at 200/s | about 95 (p50 449 ms, 32 s behind) |

Two copies did **not** raise capacity here. Everything (3 Kubernetes nodes, all services and the
load generator) shares one laptop's CPU, and Kubernetes adds its own network hops. Kong also runs
2 worker processes in k8s (to fit its memory limit) against 12 in compose, so the gateway is the
first suspect. Next: watch `kubectl top pods` during a load test, raise Kong to 4 workers with a
1 Gi limit, and measure again. Real scaling gains need more machines, not more pods on one.
