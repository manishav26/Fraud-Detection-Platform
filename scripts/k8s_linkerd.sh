#!/usr/bin/env bash
# Installs Linkerd into the k3d cluster if it isn't there yet (week 4, step 4). Safe to run again.
# Needs the linkerd CLI on the laptop (docs/mtls.md). Called by scripts/k8s_up.sh before our
# manifests, because deploy/k8s/base/mesh.yaml uses Linkerd's resource types.
set -euo pipefail

if ! command -v linkerd >/dev/null 2>&1; then
  if [ -x "$HOME/.linkerd2/bin/linkerd" ]; then
    export PATH="$HOME/.linkerd2/bin:$PATH"
  else
    echo "The linkerd CLI is missing. Install it once (docs/mtls.md):" >&2
    echo "  curl --proto '=https' --tlsv1.2 -sSfL https://run.linkerd.io/install-edge | sh" >&2
    exit 1
  fi
fi

# Fail closed: while the proxy injector isn't answering (it starts after the nodes, e.g. after the
# VM restarts), Kubernetes refuses to create a pod rather than starting it without its proxy; the
# Deployment or StatefulSet tries again until the injector is back. With Linkerd's default
# (Ignore), pods created in that window ran unmeshed and, by our mesh rules, could reach nothing:
# Kong, ingest and the feature job on 12 Nov (docs/mtls.md). kube-system is exempt, as Linkerd
# advises for this setting: with Fail, a down injector would otherwise also block CoreDNS, which
# the injector itself needs.
FAIL_CLOSED=(--set webhookFailurePolicy=Fail)
WEBHOOK=linkerd-proxy-injector-webhook-config
kubectl label namespace kube-system config.linkerd.io/admission-webhooks=disabled --overwrite >/dev/null

if kubectl get namespace linkerd >/dev/null 2>&1; then
  echo "Linkerd is installed ($(linkerd version --short --client))"
  policy=$(kubectl get mutatingwebhookconfiguration "$WEBHOOK" -o jsonpath='{.webhooks[0].failurePolicy}')
  if [ "$policy" != Fail ]; then
    echo "its proxy injector lets pods start without a proxy ($policy): upgrading to fail closed"
    linkerd upgrade "${FAIL_CLOSED[@]}" | kubectl apply -f -
  fi
else
  echo "installing Linkerd $(linkerd version --short --client)"
  # The cluster already has the Gateway API CRDs Linkerd needs (v1.4.0, from k3s's Traefik)
  linkerd check --pre
  linkerd install --crds | kubectl apply -f -
  linkerd install "${FAIL_CLOSED[@]}" | kubectl apply -f -
fi
linkerd check
policy=$(kubectl get mutatingwebhookconfiguration "$WEBHOOK" -o jsonpath='{.webhooks[0].failurePolicy}')
[ "$policy" = Fail ] || { echo "the proxy injector's failurePolicy is '$policy', not Fail" >&2; exit 1; }
echo "proxy injector: fail closed (no pod in a meshed namespace starts without its proxy)"
