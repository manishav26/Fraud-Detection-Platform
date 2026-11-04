#!/usr/bin/env bash
# Deploys the scoring path to the local k3d cluster (week 2, step 6). Safe to run again.
# Needs `make cluster` first. Docs: docs/k8s.md
set -euo pipefail
cd "$(dirname "$0")/.."

NS=fraud
IMAGES=(fraud/token-issuer:dev fraud/ingest-svc:dev fraud/scoring-svc:dev fraud/mlflow:dev
        fraud/alert-svc:dev)
PUBLIC=(kong:3.7 redpandadata/redpanda:v24.2.7 cassandra:4.1 postgres:16 rustfs/rustfs:latest)

echo "1/5 build our images and copy them into the cluster"
./scripts/gen_keys.sh
docker compose build token-issuer ingest-svc scoring-svc mlflow alert-svc
for img in "${PUBLIC[@]}"; do
  docker image inspect "$img" >/dev/null 2>&1 || docker pull "$img"
done
# Docker Desktop stores images as multi-platform lists; `k3d image import <name>` then fails with
# "content digest ... not found". Saving only this machine's platform to a tar file avoids that.
PLATFORM="linux/$(docker version --format '{{.Server.Arch}}')"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
docker save --platform "$PLATFORM" -o "$TMP/images.tar" "${IMAGES[@]}" "${PUBLIC[@]}"
k3d image import -c fraud "$TMP/images.tar"

echo "2/5 secret and config maps (from files that are not in git or live outside deploy/k8s)"
apply() { kubectl apply -f -; }
kubectl create namespace "$NS" --dry-run=client -o yaml | apply
kubectl -n "$NS" create secret generic jwt-private-key \
  --from-file=private.pem=keys/private.pem --dry-run=client -o yaml | apply
kubectl -n "$NS" create configmap kong-config \
  --from-file=kong.yml=deploy/kong/kong.yml --dry-run=client -o yaml | apply
kubectl -n "$NS" create configmap scoring-config \
  --from-env-file=deploy/scoring.env --dry-run=client -o yaml | apply
# The public half only: the private key stays on the laptop and never enters the cluster
kubectl -n "$NS" create configmap promotion-public-key \
  --from-file=public.pem=promotion-keys/public.pem --dry-run=client -o yaml | apply
kubectl -n "$NS" create configmap postgres-init \
  --from-file=deploy/postgres/init-mlflow.sql --dry-run=client -o yaml | apply
# Dev passwords, the same as compose. Encrypted secrets are week 4 work.
kubectl -n "$NS" create secret generic postgres-credentials \
  --from-literal=POSTGRES_USER=fraud --from-literal=POSTGRES_PASSWORD=fraud_dev_only \
  --dry-run=client -o yaml | apply
kubectl -n "$NS" create secret generic objectstore-credentials \
  --from-literal=RUSTFS_ACCESS_KEY=minio_dev --from-literal=RUSTFS_SECRET_KEY=minio_dev_only \
  --dry-run=client -o yaml | apply
# Only MLflow gets these: clients reach the files through MLflow, never the store directly
kubectl -n "$NS" create secret generic mlflow-credentials \
  --from-literal=MLFLOW_BACKEND_STORE_URI=postgresql+psycopg2://mlflow:mlflow_dev_only@postgres:5432/mlflow \
  --from-literal=AWS_ACCESS_KEY_ID=minio_dev --from-literal=AWS_SECRET_ACCESS_KEY=minio_dev_only \
  --dry-run=client -o yaml | apply
# alert-svc's own login: may add cases and audit rows, never change the audit log
kubectl -n "$NS" create secret generic alerts-credentials \
  --from-literal=ALERTS_DSN=postgresql://alerts:alerts_dev_only@postgres:5432/fraud \
  --dry-run=client -o yaml | apply

echo "3/5 apply the Kustomize base"
kubectl apply -k deploy/k8s/base
# config maps don't restart pods by themselves; restart ours so they read the current values
kubectl -n "$NS" rollout restart deploy/token-issuer deploy/mlflow deploy/scoring-svc deploy/ingest-svc \
  deploy/alert-svc deploy/kong

echo "4/5 wait for the data services, then create topics and the table"
kubectl -n "$NS" rollout status statefulset/redpanda --timeout=180s
kubectl -n "$NS" rollout status statefulset/cassandra --timeout=300s
kubectl -n "$NS" rollout status statefulset/postgres --timeout=180s
kubectl -n "$NS" rollout status statefulset/objectstore --timeout=180s
RPK="kubectl -n $NS exec -i redpanda-0 -- rpk" ./scripts/create_topics.sh
CQLSH="kubectl -n $NS exec -i cassandra-0 -- cqlsh" ./scripts/create_schema.sh
PSQL="kubectl -n $NS exec -i postgres-0 -- psql" ./scripts/create_db_schema.sh

echo "5/5 wait for our services (they retry until Redpanda and Cassandra answer)"
for d in mlflow token-issuer scoring-svc ingest-svc alert-svc kong; do
  kubectl -n "$NS" rollout status "deploy/$d" --timeout=300s
done
kubectl -n "$NS" get pods -o wide
echo "Gateway: http://localhost:8080"
