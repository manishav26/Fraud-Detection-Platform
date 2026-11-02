#!/bin/sh
# Starts the MLflow server. Settings come from the environment (MLFLOW_BACKEND_STORE_URI,
# MLFLOW_ARTIFACTS_DESTINATION, MLFLOW_SERVER_ALLOWED_HOSTS, ...), so compose and Kubernetes share
# this file. Design: docs/mlflow.md
set -e
python /srv/ensure_bucket.py
# One worker: a registry gets few requests, and each worker costs memory.
exec mlflow server --host 0.0.0.0 --port 5000 --workers 1
