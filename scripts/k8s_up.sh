#!/usr/bin/env bash
# Deploys the platform to the local k3d cluster. Safe to run again. Needs `make cluster` first.
# Docs: docs/k8s.md
#   scripts/k8s_up.sh                    everything: images, config, YAML, tables, restart all
#   scripts/k8s_up.sh apply [deploy...]  config maps, secrets and YAML only (no images); then
#                                        restarts the listed deployments, which read the config
#   scripts/k8s_up.sh update <service>   rebuild one of our images, copy it in, restart it
set -euo pipefail
cd "$(dirname "$0")/.."

NS=fraud
MODE=${1:-all}
# Compose and the cluster together run this laptop out of memory and stall Docker
if [ -n "$(docker compose ps -q 2>/dev/null)" ]; then
  echo "Compose is running. Stop it first: make down" >&2
  exit 1
fi
# Ours: built from this repo, so copied in on every full deploy. Each deployment has the same name.
OURS=(token-issuer ingest-svc scoring-svc mlflow alert-svc model-admin-svc)
PUBLIC=(kong:3.7 redpandadata/redpanda:v24.2.7 cassandra:4.1 postgres:16 rustfs/rustfs:latest
        prom/prometheus:v3.13.5 grafana/grafana:11.6.7)

# Copy images into every node. Docker Desktop stores images as multi-platform lists, and
# `k3d image import <name>` then fails with "content digest ... not found"; saving only this
# machine's platform to a tar file avoids that.
import_images() {
  [ $# -gt 0 ] || return 0
  local platform tmp
  platform="linux/$(docker version --format '{{.Server.Arch}}')"
  tmp=$(mktemp -d)
  docker save --platform "$platform" -o "$tmp/images.tar" "$@"
  k3d image import -c fraud "$tmp/images.tar"
  rm -rf "$tmp"
}

# A public image never changes under its tag, so copy it in only if a node doesn't have it yet.
# Copying all of them every time (a few GB into 3 nodes) was most of a deploy's time.
missing_public() {
  local img node
  for img in "${PUBLIC[@]}"; do
    for node in $(docker ps -q --filter label=k3d.cluster=fraud --filter label=k3d.role=server) \
                $(docker ps -q --filter label=k3d.cluster=fraud --filter label=k3d.role=agent); do
      if ! docker exec "$node" crictl inspecti -q "$img" >/dev/null 2>&1; then
        echo "$img"
        break
      fi
    done
  done
}

wait_for() {   # wait_for <deployment>...
  local d
  for d in "$@"; do
    kubectl -n "$NS" rollout status "deploy/$d" --timeout=300s
  done
}

if [ "$MODE" = update ]; then
  SVC=${2:-}
  if [[ ! " ${OURS[*]} " == *" $SVC "* ]]; then
    echo "usage: scripts/k8s_up.sh update <one of: ${OURS[*]}>" >&2
    exit 1
  fi
  echo "update $SVC: build, copy into the cluster, restart"
  docker compose build "$SVC"
  import_images "fraud/$SVC:dev"
  kubectl -n "$NS" rollout restart "deploy/$SVC"
  wait_for "$SVC"
  kubectl -n "$NS" get pods -l "app=$SVC"
  exit 0
fi

if [ "$MODE" = all ]; then
  echo "1/5 build our images and copy them into the cluster (public ones only if missing)"
  ./scripts/gen_keys.sh
  docker compose build "${OURS[@]}"
  for img in "${PUBLIC[@]}"; do
    docker image inspect "$img" >/dev/null 2>&1 || docker pull "$img"
  done
  IMAGES=()
  for svc in "${OURS[@]}"; do IMAGES+=("fraud/$svc:dev"); done
  NEED=$(missing_public)
  if [ -n "$NEED" ]; then
    echo "public images not in the cluster yet: $(echo $NEED)"
    # shellcheck disable=SC2206   # one image name per line, no spaces in names
    IMAGES+=($NEED)
  else
    echo "public images: all already in the cluster, skipped"
  fi
  import_images "${IMAGES[@]}"
fi

if [ "$MODE" = apply ]; then
  ./scripts/gen_keys.sh   # rewrites deploy/kong/kong.yml from its template; keeps existing keys
elif [ "$MODE" != all ]; then
  echo "unknown mode $MODE: use no argument, apply, or update <service>" >&2
  exit 1
fi

echo "2/5 secret and config maps (from files that are not in git or live outside deploy/k8s)"
apply() { kubectl apply -f -; }
kubectl create namespace "$NS" --dry-run=client -o yaml | apply
kubectl -n "$NS" create secret generic jwt-private-key \
  --from-file=private.pem=keys/private.pem --dry-run=client -o yaml | apply
kubectl -n "$NS" create secret generic issuer-clients \
  --from-file=clients.issuer.json=keys/clients.issuer.json --dry-run=client -o yaml | apply
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
# Metrics: Prometheus's config and Grafana's data source, dashboard loader and dashboard
kubectl -n "$NS" create configmap prometheus-config \
  --from-file=prometheus.yml=deploy/prometheus/prometheus.k8s.yml --dry-run=client -o yaml | apply
kubectl -n "$NS" create configmap grafana-datasources \
  --from-file=deploy/grafana/datasources.yml --dry-run=client -o yaml | apply
kubectl -n "$NS" create configmap grafana-providers \
  --from-file=deploy/grafana/dashboards.yml --dry-run=client -o yaml | apply
kubectl -n "$NS" create configmap grafana-dashboards \
  --from-file=deploy/grafana/dashboards/ --dry-run=client -o yaml | apply
kubectl -n "$NS" create secret generic grafana-admin \
  --from-literal=GF_SECURITY_ADMIN_USER=admin --from-literal=GF_SECURITY_ADMIN_PASSWORD=grafana_dev_only \
  --dry-run=client -o yaml | apply
# alert-svc's own login: may add cases and audit rows, never change the audit log
kubectl -n "$NS" create secret generic alerts-credentials \
  --from-literal=ALERTS_DSN=postgresql://alerts:alerts_dev_only@postgres:5432/fraud \
  --dry-run=client -o yaml | apply
# model-admin-svc's own login: may only read and add audit rows
kubectl -n "$NS" create secret generic model-admin-credentials \
  --from-literal=MODEL_ADMIN_DSN=postgresql://model_admin:model_admin_dev_only@postgres:5432/fraud \
  --dry-run=client -o yaml | apply

echo "3/5 apply the Kustomize base"
# Pods whose YAML changed restart by themselves; a changed config map alone restarts nothing
kubectl apply -k deploy/k8s/base

if [ "$MODE" = apply ]; then
  shift
  if [ $# -gt 0 ]; then
    echo "restart, to read the current config: $*"
    for d in "$@"; do kubectl -n "$NS" rollout restart "deploy/$d"; done
    wait_for "$@"
  fi
  kubectl -n "$NS" get pods
  exit 0
fi

# A full deploy restarts all of ours, so every pod reads the current config and image
kubectl -n "$NS" rollout restart deploy/token-issuer deploy/mlflow deploy/scoring-svc deploy/ingest-svc \
  deploy/alert-svc deploy/model-admin-svc deploy/kong deploy/prometheus deploy/grafana

echo "4/5 wait for the data services, then create topics and the table"
kubectl -n "$NS" rollout status statefulset/redpanda --timeout=180s
kubectl -n "$NS" rollout status statefulset/cassandra --timeout=300s
kubectl -n "$NS" rollout status statefulset/postgres --timeout=180s
kubectl -n "$NS" rollout status statefulset/objectstore --timeout=180s
RPK="kubectl -n $NS exec -i redpanda-0 -- rpk" ./scripts/create_topics.sh
CQLSH="kubectl -n $NS exec -i cassandra-0 -- cqlsh" ./scripts/create_schema.sh
PSQL="kubectl -n $NS exec -i postgres-0 -- psql" ./scripts/create_db_schema.sh

echo "5/5 wait for our services (they retry until Redpanda and Cassandra answer)"
wait_for mlflow token-issuer scoring-svc ingest-svc alert-svc model-admin-svc kong prometheus grafana
kubectl -n "$NS" get pods -o wide
echo "Gateway: http://localhost:8080"
