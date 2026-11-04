# Metrics (week 3, step 7)

Each service has a `/metrics` page that Prometheus reads every few seconds. It is reachable inside the
network only: Kong never routes it, because the numbers reveal internals. Prometheus and Grafana
come in the next commits.

## What each service reports

| Metric | From | Shows |
|---|---|---|
| `http_requests_total{service, route, method, status}` | every service | requests per second and error rate, per route |
| `http_request_duration_seconds{service, route, method}` | every service | p50, p95, p99 per route |
| `scoring_step_seconds{step}` | scoring | time in `history_read`, `predict`, `reasons`, `history_write` |
| `scoring_decisions_total{decision, cold_start, degraded}` | scoring | approve, review and decline; how many were degraded |
| `scoring_model_info{version}` | scoring | the model answering right now, updated after a promotion |
| `history_breaker_open` | scoring | 1 while the circuit breaker refuses calls to Cassandra |
| `history_calls_total{op, result}` | scoring | Cassandra reads and writes: `ok`, `failed`, `refused` |
| `history_skipped_writes_total` | scoring | payments missing from history (gaps) |
| `ingest_step_seconds{step}` | ingest | `publish_raw` (Redpanda) and `wait_scoring` |
| `ingest_scored_publish_failures_total` | ingest | decisions that never reached `txn.scored`, so no case opened |
| `alerts_messages_total{outcome}` | alert-svc | `cases_opened`, `already_open`, `not_flagged`, `unreadable` |

- **Route patterns, not paths:** `/cases/{case_id}`, so each case id doesn't create its own series.
- **The `/metrics` page doesn't count itself.**
- **Step times only where they apply:** an unknown user's read takes 0 ms (no Cassandra call), so it
  isn't counted, or it would hide slow reads. Reasons are timed only for flagged payments.
- **These replace earlier stand-ins:** the breaker's log lines (unreliable, see
  `docs/results/safe_reads.md`) and alert-svc's `/stats`. `make alert-stats` now reads `/metrics`.

## Why these

The plan asks for requests per second, p50, p95, p99 and errors per service. The step times and
breaker metrics are here to answer three open questions: the unexplained slow periods, the 8%
degraded payments seen on the cluster, and which hop is slow (Kong, ingest, Redpanda, Cassandra or
the model).
