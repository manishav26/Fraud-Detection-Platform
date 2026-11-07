# Gate 2: platform side

## 1. Goal
The platform takes a payment through a secure gateway, scores it in real time, and answers approve,
review or decline with reasons. It opens a case for every flagged payment, records every analyst
action in a log that can't be changed quietly, and lets a new model go live without a restart. I
judge it by end-to-end latency and errors under real load. For security, each control needs a test
with a pass number, or it is only a claim.

## 2. How it is built
Kong checks each request's token, role and rate, then passes payments to ingest. Ingest asks
scoring for a decision, then publishes the payment and the decision to Redpanda. Scoring reads the
user's history from Cassandra, runs Manisha's model and adds the top 3 reasons. alert-svc reads
the decisions and opens cases in Postgres, each with an audit row. MLflow keeps the models, and
Prometheus and Grafana show the metrics. Everything runs on Docker Compose and on a 3-node k3d
Kubernetes cluster, with 2 copies each of ingest, scoring and alert-svc. The demo runs on the
cluster.

## 3. Security, tested
The threat model lists 14 threats, each with a control and a test. Five tests have run so far:
- **ST1 (bad tokens):** 1,000 forged, expired, altered, `alg: none` and similar tokens all got
  401, and none reached ingest.
- **ST2 (roles):** client, analyst and admin each called every route, and all 16 answers match
  the roles table.
- **ST7 (flooding):** a client sending past its 50/s limit gets 429. Whether this protects
  *other* clients can't be judged on one laptop, because run-to-run noise is larger than the 10%
  pass rule.
- **ST8 (audit tampering):** a row edited by the database owner is named exactly by the checker.
- **ST9 (model swap):** a model file changed after signing, a forged promotion and a replayed one
  are all refused, and the current model keeps serving.

## 4. Changing the model safely
A model's file hashes are pinned in the deploy config, outside the model folder. Going live needs
a promotion message signed with a key that never enters a container. Scoring checks the signature
and the hashes, then swaps the model while serving. On the cluster, both pods swapped in about
0.3 s with 3,000 of 3,000 payments answered.

## 5. Staying up when Cassandra fails
History reads have a 150 ms limit and a circuit breaker. With Cassandra stopped under load, every
payment was still answered (p95 58 ms), marked `degraded`. On real payments, that changed 21 of
5,000 decisions and declined no extra genuine customers. Loading days 1–109 of history (337,614
payments) halved the cold starts.

## 6. Cases and the audit log
Replaying 2,000 payments opened exactly 42 cases, one per flagged payment. Replaying them again
opened none. With one alert-svc copy killed during a replay, no case was lost or opened twice.
Each audit row holds the hash of the row before it, so a changed row breaks the chain at that
point.

## 7. Baseline
Real payments were sent at exactly 30 per second for 10 minutes, 3 times on the cluster. End to
end through Kong: p50 20 ms, **p95 188 ms** (103–334 ms across runs), p99 905 ms. There was no
5xx from any service, and 0.2% of payments were scored without history. CPU was not the limit
(peak 1.8 cores). The first minute after a deploy is much slower (p95 1.5 s), so the demo needs
a warm-up.

## 8. Open issues and next steps
- Short slow bursts (one or two bad minutes per run) on both setups. The cause isn't known yet.
- ST7's isolation half: to be rerun from a separate machine (week 5).
- 5 gateway errors in 72,000 cluster payments that ingest never saw.
- The audit log doesn't yet detect rows removed from the end. The fix is keeping the latest hash
  outside the database.
- `model_version` doesn't change after a promotion. Next PR: a short hash in the label, and pins
  for Manisha's tuned v1.
- Week 4: mTLS between services (ST3), network rules so each pod reaches only what it needs (ST4),
  dependency scanning in CI (ST10), and autoscaling.
- Week 5: input fuzzing and injection tests (ST5, ST6), checks on personal data in logs (ST11),
  replay protection (ST12), and failure tests on a rented VM.
