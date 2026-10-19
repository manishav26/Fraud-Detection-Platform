# Fraud-Detection-Platform

**A real-time fraud detection system that notices when it is going out of date, and fixes itself.**

## About

Every card payment needs an instant decision: approve, review or decline. Most banks make it with
a machine learning model, but fraud keeps changing, so a model trained months ago slowly misses more fraud. 
This is called **concept drift**.

Our platform scores each transaction in real time, and it also watches its own accuracy as fraud labels arrive. When it detects drift, it retrains on recent data and swaps in the new model without stopping.

The main question we answer: **is retraining only when drift is detected as good as retraining on a fixed schedule, with fewer retrains?**

The model sits inside a scalable, secure platform (microservices, streaming, Kubernetes, authentication, encryption), and we test every part with real numbers.

**Data:** 
IEEE-CIS Fraud Detection (Kaggle), about 590K real card transactions over six months.

## Team

| | Focus |
|---|---|
| **Urmi Shah** | Platform and security: services, streaming, storage, Kubernetes, security controls |
| **Manisha Varthamanan** | Machine learning and drift: data, features, models, drift detection, experiments |

## Run locally
`make up` starts the stack (needs Docker). Get a token and score a transaction:
```
TOKEN=$(curl -s -X POST localhost:8000/token | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -s -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"txn_id":"t1","user_id":"u1","amount":10,"ts":"2026-10-19T10:00:00Z"}' localhost:8000/v1/transactions/score
```
Without a token the gateway returns 401. `make test` and `make lint` run the checks.
