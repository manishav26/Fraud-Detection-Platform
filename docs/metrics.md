# Metrics (week 3, step 7)

Each service has a `/metrics` page that Prometheus reads every 5 seconds. It is reachable inside the
network only: Kong never routes it, because the numbers reveal internals.

**On the cluster:** `make k8s-grafana`, then http://localhost:3000 (`admin` / `grafana_dev_only`),
dashboard "Fraud platform". `make k8s-prometheus` shows what Prometheus reads, at
http://localhost:9090/targets. Compose comes in the next commit.

## How it fits together

| Piece | How |
|---|---|
| Finding what to read | Pods marked `prometheus.io/scrape: "true"` in `fraud`, found by asking Kubernetes, so new copies are read automatically. Its read-only access to pods and nodes is in `deploy/k8s/base/prometheus.yaml` |
| Kong | Its `prometheus` plugin, on a separate status port (8100) that isn't routed outside |
| Redpanda | Its built-in `/public_metrics` |
| CPU and memory | Each node's kubelet (cAdvisor), through the Kubernetes API. Only the 4 series the dashboard uses are kept |
| Every 5 s, 2 days kept | Short enough to catch slow periods of about 10 s; little memory |
| Dashboard | `deploy/grafana/dashboards/platform.json`, loaded at startup, edited in git rather than by clicking |
| Versions | Prometheus v3.13.5 and Grafana 11.6.7, pinned. Grafana 13 was tried first: its new storage and search index used over 7 CPU cores at startup and never became ready |
| CPU caps | 1 core each for Prometheus and Grafana, so measuring the platform can't slow it down |

## The dashboard

1. Requests per second, by service
2. Error rate (5xx share), by service
3. Latency p50, p95 and p99, by service
4. Scoring: p95 per step
5. Ingest: p95 per step
6. Decisions per second, and degraded
7. Cassandra: breaker, failed and refused calls, skipped writes
8. Kong: answers by status code (401, 403, 413, 429)
9. Kong: its own latency vs the service behind it
10. Cases: `txn.scored` messages by outcome
11. Live model
12. CPU per pod
13. CPU per node
14. Memory per pod

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

## First reading (4 Nov, replay on the cluster)

- **Cassandra pauses explain the breaker opening.** Its log shows Java memory-cleanup pauses
  (`GCInspector`) of 956 ms and 636 ms during the replay. Reads are capped at 150 ms, so every read
  during a pause fails, the breaker opens, and those payments are scored `degraded`. The dashboard
  showed failed reads, refused writes and skipped writes at the same moments.
- **Ingest waits far longer than scoring works.** `wait_scoring` p95 was about 1.2 s while each of
  scoring's own steps stayed under about 200 ms. The time is spent queueing (in Redpanda or waiting
  for CPU), not in the model. Kong's upstream p95 for ingest was about 2 s.
- **CPU per node peaked near 2.7 cores**, so the three services, Cassandra and Redpanda share too
  little CPU on the laptop. This matches the slow periods; week 5's tests on a rented VM check it.
- Kong returned a few 500 and 502 answers at the same moments. Week 3's load test (step 8) counts them.
- Pods replaced by a restart stay in the CPU and memory legends for the time range shown; their
  lines simply stop.
