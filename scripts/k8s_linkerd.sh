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

if kubectl get namespace linkerd >/dev/null 2>&1; then
  echo "Linkerd is installed ($(linkerd version --short --client))"
else
  echo "installing Linkerd $(linkerd version --short --client)"
  # The cluster already has the Gateway API CRDs Linkerd needs (v1.4.0, from k3s's Traefik)
  linkerd check --pre
  linkerd install --crds | kubectl apply -f -
  linkerd install | kubectl apply -f -
fi
linkerd check
