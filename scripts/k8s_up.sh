#!/usr/bin/env bash
# Deploys the platform to the local k3d cluster. Safe to run again. Needs `make cluster` first.
# Docs: docs/k8s.md
#   scripts/k8s_up.sh                    everything: images, config, YAML, tables, restart all
#   scripts/k8s_up.sh apply [deploy...]  config maps, secrets and YAML only (no images); then
#                                        restarts the listed deployments, which read the config
#   scripts/k8s_up.sh update <service>   rebuild one of our images, copy it in, restart it
# Passwords come from deploy/k8s/secrets.enc.env (make k8s-secrets; needs sops and your age key).
# A full deploy also changes the database logins to match them (docs/secrets.md).
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
# The passwords, decrypted into memory only (docs/secrets.md)
# shellcheck source=scripts/k8s_secrets.sh
source scripts/k8s_secrets.sh
load_secrets
secret_value() {   # secret_value <secret> <key>: what the cluster has now, empty if nothing
  kubectl -n "$NS" get secret "$1" -o "jsonpath={.data.$2}" 2>/dev/null | base64 -d 2>/dev/null || true
}
# A fingerprint of the passwords the logins were last changed to. Not secret: a SHA-256 of 192-bit
# random values. Lets `apply` refuse when the file has new passwords the logins don't have yet.
FINGERPRINT=$(decrypt_secrets | shasum -a 256 | cut -c1-16)
APPLIED=$(kubectl -n "$NS" get configmap secrets-fingerprint -o jsonpath='{.data.fingerprint}' 2>/dev/null || true)
if [ "$MODE" = apply ] && [ "$APPLIED" != "$FINGERPRINT" ]; then
  echo "The passwords in $SECRETS_FILE aren't the ones the logins were last set to (or never" >&2
  echo "were), and only a full deploy changes the logins. Run: make k8s-up" >&2
  exit 1
fi
# Before a rotation: what the cluster has now, so the old logins can still be used to change them
PREVIOUS_CASSANDRA_ADMIN_PASSWORD=$(secret_value cassandra-admin-credentials CASSANDRA_ADMIN_PASSWORD)
PREVIOUS_OBJECTSTORE_SECRET_KEY=$(secret_value objectstore-credentials RUSTFS_SECRET_KEY)
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
# Each Secret is built from the decrypted values in memory and sent on stdin: no file, nothing
# on the command line. secret <name> <KEY=value>...
secret() {
  local name=$1; shift
  kubectl -n "$NS" create secret generic "$name" --from-env-file=<(printf '%s\n' "$@") \
    --dry-run=client -o yaml | apply
}
secret postgres-credentials POSTGRES_USER=fraud "POSTGRES_PASSWORD=$POSTGRES_PASSWORD"
secret objectstore-credentials RUSTFS_ACCESS_KEY=minio_dev "RUSTFS_SECRET_KEY=$OBJECTSTORE_SECRET_KEY"
# Only MLflow gets these: clients reach the files through MLflow, never the store directly
secret mlflow-credentials \
  "MLFLOW_BACKEND_STORE_URI=postgresql+psycopg2://mlflow:$MLFLOW_DB_PASSWORD@postgres:5432/mlflow" \
  AWS_ACCESS_KEY_ID=minio_dev "AWS_SECRET_ACCESS_KEY=$OBJECTSTORE_SECRET_KEY"
# Metrics: Prometheus's config and Grafana's data source, dashboard loader and dashboard
kubectl -n "$NS" create configmap prometheus-config \
  --from-file=prometheus.yml=deploy/prometheus/prometheus.k8s.yml --dry-run=client -o yaml | apply
kubectl -n "$NS" create configmap grafana-datasources \
  --from-file=deploy/grafana/datasources.yml --dry-run=client -o yaml | apply
kubectl -n "$NS" create configmap grafana-providers \
  --from-file=deploy/grafana/dashboards.yml --dry-run=client -o yaml | apply
kubectl -n "$NS" create configmap grafana-dashboards \
  --from-file=deploy/grafana/dashboards/ --dry-run=client -o yaml | apply
# Grafana keeps no state between restarts (emptyDir), so it reads this password on every start
secret grafana-admin GF_SECURITY_ADMIN_USER=admin "GF_SECURITY_ADMIN_PASSWORD=$GRAFANA_ADMIN_PASSWORD"
# alert-svc's own login: may add cases and audit rows, never change the audit log
secret alerts-credentials "ALERTS_DSN=postgresql://alerts:$ALERTS_DB_PASSWORD@postgres:5432/fraud"
# scoring-svc's own Cassandra login: may only read and write txn_by_user (docs/cassandra.md)
secret cassandra-credentials CASSANDRA_USER=scoring "CASSANDRA_PASSWORD=$CASSANDRA_SCORING_PASSWORD"
# model-admin-svc's own login: may only read and add audit rows
secret model-admin-credentials "MODEL_ADMIN_DSN=postgresql://model_admin:$MODEL_ADMIN_DB_PASSWORD@postgres:5432/fraud"

# RustFS reads its key only when it starts: restart it if the key changed
if [ -n "$PREVIOUS_OBJECTSTORE_SECRET_KEY" ] && [ "$PREVIOUS_OBJECTSTORE_SECRET_KEY" != "$OBJECTSTORE_SECRET_KEY" ]; then
  echo "restart objectstore: its key changed"
  kubectl -n "$NS" rollout restart statefulset/objectstore
fi

echo "3/5 Linkerd (if missing), then apply the Kustomize base"
./scripts/k8s_linkerd.sh
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

# The data stores get their Linkerd proxy only when their pod is created: restart any that has
# none yet (once, the first deploy after Linkerd; docs/mtls.md)
for sts in redpanda cassandra postgres objectstore; do
  if ! kubectl -n "$NS" get pod "$sts-0" -o jsonpath='{.spec.containers[*].name} {.spec.initContainers[*].name}' \
       2>/dev/null | grep -q linkerd-proxy; then
    echo "restart $sts: it has no Linkerd proxy yet"
    kubectl -n "$NS" rollout restart "statefulset/$sts"
  fi
done

echo "4/5 wait for the data services, create topics and tables, set the logins' passwords"
kubectl -n "$NS" rollout status statefulset/redpanda --timeout=180s
kubectl -n "$NS" rollout status statefulset/cassandra --timeout=300s
kubectl -n "$NS" rollout status statefulset/postgres --timeout=180s
kubectl -n "$NS" rollout status statefulset/objectstore --timeout=180s
RPK="kubectl -n $NS exec -i redpanda-0 -- rpk" ./scripts/create_topics.sh
# Also sets both Cassandra logins to the passwords from the file (from the previous ones if rotating)
CQLSH="kubectl -n $NS exec -i cassandra-0 -- cqlsh" \
  PREVIOUS_CASSANDRA_ADMIN_PASSWORD="$PREVIOUS_CASSANDRA_ADMIN_PASSWORD" ./scripts/create_schema.sh
# No pod uses this one; kept so the next rotation knows the admin's current password
secret cassandra-admin-credentials "CASSANDRA_ADMIN_PASSWORD=$CASSANDRA_ADMIN_PASSWORD"
PSQL="kubectl -n $NS exec -i postgres-0 -- psql" ./scripts/create_db_schema.sh
# Postgres: psql inside the pod logs in over the local socket without a password, so the logins
# can be set whatever they were before (the dev ones the first time). Sent on stdin.
kubectl -n "$NS" exec -i postgres-0 -- psql -U fraud -d fraud -v ON_ERROR_STOP=1 -q <<SQL
ALTER ROLE fraud PASSWORD '$POSTGRES_PASSWORD';
ALTER ROLE mlflow PASSWORD '$MLFLOW_DB_PASSWORD';
ALTER ROLE alerts PASSWORD '$ALERTS_DB_PASSWORD';
ALTER ROLE model_admin PASSWORD '$MODEL_ADMIN_DB_PASSWORD';
SQL
echo "logins set: Postgres fraud, mlflow, alerts, model_admin; Cassandra fraud_admin, scoring"
kubectl -n "$NS" create configmap secrets-fingerprint --from-literal=fingerprint="$FINGERPRINT" \
  --dry-run=client -o yaml | apply

# A full deploy restarts all of ours, so every pod reads the current config, image and
# passwords. After the logins above, so no pod starts with a password the database doesn't know.
kubectl -n "$NS" rollout restart deploy/token-issuer deploy/mlflow deploy/scoring-svc deploy/ingest-svc \
  deploy/alert-svc deploy/model-admin-svc deploy/kong deploy/prometheus deploy/grafana

echo "5/5 wait for our services (they retry until Redpanda and Cassandra answer)"
wait_for mlflow token-issuer scoring-svc ingest-svc alert-svc model-admin-svc kong prometheus grafana
kubectl -n "$NS" get pods -o wide
echo "Gateway: http://localhost:8080"
