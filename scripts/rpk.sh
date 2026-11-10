#!/usr/bin/env bash
# rpk inside the cluster's Redpanda pod, as the superuser `admin` once logins are on (docs/kafka.md)
#
#   scripts/rpk.sh topic list
#   RPK=./scripts/rpk.sh ./scripts/create_topics.sh
#
# The password comes from the shell (REDPANDA_ADMIN_PASSWORD, as scripts/k8s_up.sh has it) or
# from the cluster's redpanda-admin Secret. It goes into the pod on stdin and into rpk's
# environment there: never on a command line. While logins are still off (a new cluster, before
# scripts/kafka_security.sh), rpk runs without one: Redpanda refuses a login it isn't expecting.
set -euo pipefail
NS=${NS:-fraud}
in_pod() { kubectl -n "$NS" exec -i redpanda-0 -c redpanda -- "$@"; }

# No -i here: this check must not swallow what is piped in for rpk
if [ "$(kubectl -n "$NS" exec redpanda-0 -c redpanda -- rpk cluster config get enable_sasl \
        2>/dev/null)" != true ]; then
  exec kubectl -n "$NS" exec -i redpanda-0 -c redpanda -- rpk "$@"
fi
PASSWORD=${REDPANDA_ADMIN_PASSWORD:-$(kubectl -n "$NS" get secret redpanda-admin \
  -o jsonpath='{.data.REDPANDA_ADMIN_PASSWORD}' | base64 -d)}
[ -n "$PASSWORD" ] || { echo "no Redpanda admin password: run make k8s-up" >&2; exit 1; }
# The first line of stdin is the password; anything piped in after it reaches rpk unchanged
if [ -t 0 ]; then
  printf '%s\n' "$PASSWORD"
else
  { printf '%s\n' "$PASSWORD"; cat; }
fi | in_pod sh -c 'IFS= read -r RPK_PASS; export RPK_PASS RPK_USER=admin RPK_SASL_MECHANISM=SCRAM-SHA-256
exec rpk "$@"' rpk "$@"
