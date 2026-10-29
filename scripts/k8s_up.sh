#!/usr/bin/env bash
# Deploys the scoring path to the local k3d cluster (week 2, step 6). Safe to run again.
# Needs `make cluster` first. Docs: docs/k8s.md
set -euo pipefail
cd "$(dirname "$0")/.."

NS=fraud
IMAGES=(fraud/token-issuer:dev fraud/ingest-svc:dev fraud/scoring-svc:dev)
PUBLIC=(kong:3.7 redpandadata/redpanda:v24.2.7 cassandra:4.1)

echo "1/5 build our images and copy them into the cluster"
./scripts/gen_keys.sh
docker compose build token-issuer ingest-svc scoring-svc
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

echo "3/5 apply the Kustomize base"
kubectl apply -k deploy/k8s/base
# config maps don't restart pods by themselves; restart ours so they read the current values
kubectl -n "$NS" rollout restart deploy/token-issuer deploy/scoring-svc deploy/ingest-svc deploy/kong

echo "4/5 wait for Redpanda and Cassandra, then create topics and the table"
kubectl -n "$NS" rollout status deploy/redpanda --timeout=180s
kubectl -n "$NS" rollout status deploy/cassandra --timeout=300s
RPK="kubectl -n $NS exec -i deploy/redpanda -- rpk" ./scripts/create_topics.sh
CQLSH="kubectl -n $NS exec -i deploy/cassandra -- cqlsh" ./scripts/create_schema.sh

echo "5/5 wait for our services (they retry until Redpanda and Cassandra answer)"
for d in token-issuer scoring-svc ingest-svc kong; do
  kubectl -n "$NS" rollout status "deploy/$d" --timeout=300s
done
kubectl -n "$NS" get pods -o wide
echo "Gateway: http://localhost:8080"
