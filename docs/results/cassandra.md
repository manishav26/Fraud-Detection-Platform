# Cassandra speed test (week 2, step 2)

Single local Cassandra 4.1 node (1 GB heap) in Docker on a MacBook, `make cassandra-speed`.

Bulk load: 10,000 writes in 3.81 s (2,627 writes/s, 64 in flight)

| Operation | Calls | p50 ms | p99 ms |
|---|---|---|---|
| Write one payment (upsert) | 1,000 | 1.32 | 4.24 |
| Read one user's earlier payments | 1,000 | 2.13 | 5.95 |

Upsert check: user speedtest-0 has 10 rows after rewrites (expected 10)
