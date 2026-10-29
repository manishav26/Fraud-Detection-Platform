# Load tests (week 2, step 4)

Local Docker stack on a MacBook: Kong -> ingest-svc (1 worker) -> Redpanda (1 core) + scoring-svc
(stub). Synthetic payments (`make load`), open loop, 64 in flight.

## Run 1: baseline

| Target rate | Achieved | Status codes | p50 ms | p95 ms | p99 ms | Furthest behind |
|---|---|---|---|---|---|---|
| 50/s for 30 s | 50.0/s | 1,500 x 200 | 22.2 | 151.0 | 248.5 | 0.08 s |
| 200/s for 30 s | **52.3/s** | 5,999 x 200, 1 x 502 | 846.8 | 2,977.6 | 4,724.9 | **83.3 s** |

**Reading it:** the platform tops out at about 52 payments per second. At 50/s it keeps up; at
200/s it still answers only about 52/s, so requests queue up (83 s behind schedule after 30 s)
and latency grows into seconds. One request got a 502 from Kong under that queue.

Every accepted payment reached Redpanda: `txn.raw` held 9,501 messages over 6 partitions
(1,509 to 1,762 each, so `user_id` keys spread evenly).

**Likely bottleneck:** ingest-svc opened a new HTTP client, and so a new connection, to
scoring-svc for every request. Fix: one shared client that reuses connections. Run 2 measures it.

## Run 2: shared HTTP client in ingest

| Target rate | Achieved | Status codes | p50 ms | p95 ms | p99 ms | Furthest behind |
|---|---|---|---|---|---|---|
| 50/s for 30 s | 50.0/s | 1,500 x 200 | 8.6 | 12.8 | 18.7 | 0.06 s |
| 200/s for 30 s | 162.9/s | 6,000 x 200 | 254.6 | 947.3 | 1,430.9 | 6.5 s |
| 400/s for 30 s | 152.7/s | 12,000 x 200 | 295.3 | 1,060.3 | 1,653.2 | 48.2 s |

## Before and after

| | Run 1 | Run 2 | Change |
|---|---|---|---|
| Most payments per second | about 52 | about 155-163 | **about 3x** |
| p99 at 50/s | 248.5 ms | 18.7 ms | **13x lower** |
| Errors | 1 x 502 | none | |

**What we learned:** one line of setup per request (a new HTTP client) was the main cost. Reusing
connections tripled capacity and made normal-load latency 13x steadier. The new ceiling is about
155 payments/s on one laptop; the next suspects are the single ingest worker process and the load
generator itself running on the same machine. On Kubernetes (step 6) we can add replicas of
ingest and measure again.
