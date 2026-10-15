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
