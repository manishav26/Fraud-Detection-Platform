# Fraud-Detection-Platform

**A real-time fraud detection system that notices when it is going out of date, and fixes itself.**

## About

Every card payment needs an instant decision: approve, review or decline. Most banks make it with
a machine learning model, but fraud keeps changing, so a model trained months ago slowly misses more fraud.
This is called **concept drift**.

Our platform scores each transaction in real time, and it also watches its own accuracy as fraud labels arrive. When it detects drift, it retrains on recent data and swaps in the new model without stopping.

The main question we answer: **is retraining only when drift is detected as good as retraining on a fixed schedule, with fewer retrains?**

**Answer:** as good, but not with fewer retrains on this data. Both kinds of retraining recover about +0.04 PR-AUC over a model that is never retrained. Drift-triggered retraining matched a 30-day schedule (0.534 vs 0.532, within seed noise) but retrained 4 times instead of 2, because this data keeps drifting.

The model sits inside a scalable, secure platform (microservices, streaming, Kubernetes, authentication, encryption), and we test every part with real numbers.

**Data:** IEEE-CIS Fraud Detection (Kaggle), about 590K real card transactions over six months, split by time: train on days 1-73, choose thresholds on 74-91, test on 92-109, replay 110-182 as a live stream. The data is not in this repository; [data/README.md](data/README.md) explains how to download it.

## How it works

1. A client sends a payment to the **gateway** (Kong), which checks its token, role and rate limit.
2. **ingest-svc** validates it, replaces the user id with a pseudonym and passes it on (Kafka).
3. **scoring-svc** reads the user's payment history from **Cassandra**, scores the payment with a **LightGBM** model, explains it (top 3 reasons) and decides: approve, review or decline. Clients see only the decision.
4. Flagged payments become cases for analysts, and every action goes to a tamper-evident **audit log**.
5. Fraud labels arrive 14 days late; **drift detection** (PSI on inputs, ADWIN on missed fraud) says when to retrain.
6. A new model is registered in **MLflow** with its settings, data and metrics, must pass a **promotion gate**, and goes live through a signed promotion without stopping the service.

It runs with Docker Compose on a laptop and on Kubernetes (k3d, 3 nodes), with Prometheus and Grafana for monitoring.

## Results

**Machine learning** ([ml/README.md](ml/README.md), [docs/results/](docs/results/))
- Model v1: PR-AUC 0.498 (random is 0.035), catches 43.4% of fraud at 1% false alarms, flags 1.3% of payments (within the analysts' 2% budget). Scoring 0.15 ms, explanations 2.9 ms per payment.
- Training and live scoring compute identical features on 10,000 of 10,000 payments.
- Feature PSI catches broken inputs within a day; only ADWIN on missed fraud catches concept drift (18 days, including the 14-day label delay).
- A model retrained on recent days (v2) is +0.065 PR-AUC better on unseen later days, but flags 2.27% of payments, so the promotion gate kept the live model.
- Hiding the score and probe detection make evasion attacks harder; tested live, attackers still win about half the time before probe detection starts, because their own tries become card history (future work).

**Platform and security** ([docs/results/](docs/results/))
- 30 payments/s end to end on the laptop cluster: p95 188 ms. On a 3-node cloud VM: up to 150 payments/s within all targets (p95 97 ms), and about 450/s with 16 cores.
- A killed scoring pod or Cassandra node: 0 of 43,200 payments noticed.
- Security tests pass for forged tokens, roles, mTLS between services, network isolation, fuzzing (2,240 cases), injection, a tamper-evident audit log, supply-chain scans, personal data (no raw user ids stored) and replayed requests.

## ML pipeline

How to run the data prep, experiments and model training: see [ml/README.md](ml/README.md).

## Team

| | Focus |
|---|---|
| **Urmi Shah** | Platform and security: services, streaming, storage, Kubernetes, security controls |
| **Manisha Varthamanan** | Machine learning and drift: data, features, models, drift detection, experiments |

## Run locally

`make up` starts the stack (needs Docker). Get a token and score a transaction:
```
TOKEN=$(make -s token CLIENT=merchant-1)   # clients and roles: docs/access.md
curl -s -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"txn_id":"t1","user_id":"u1","amount":10,"ts":"2026-10-19T10:00:00Z"}' localhost:8000/v1/transactions/score
```
The answer contains only the transaction id and the decision. Without a token the gateway returns 401. `make test` and `make lint` run the checks; `make replay` sends real payments from the stream days.

Passwords in `compose.yaml` (ending in `_dev_only`) are for local development only. The Kubernetes setup uses its own passwords, encrypted with SOPS ([docs/secrets.md](docs/secrets.md)).

## Object store and Kubernetes

- `make up` also starts an S3-compatible object store (RustFS) on port 9000, console on http://localhost:9001, login `minio_dev` / `minio_dev_only`. MLflow keeps its model files there. MinIO was dropped because it no longer publishes free images or binaries.
- `make cluster` creates a local k3d Kubernetes cluster named `fraud`, `make k8s-up` deploys the platform to it, and `make cluster-down` removes it ([docs/k8s.md](docs/k8s.md)).

## License

Code: [MIT](LICENSE). The IEEE-CIS dataset is not included and is subject to Kaggle's competition rules.
