#!/usr/bin/env bash
# One-time setup of the week 5 measurement VM (Ubuntu 24.04, x86_64). Design: docs/vm.md
#
#   ssh fraud-vm 'bash -s' < scripts/vm_setup.sh      (from the laptop, once, after launch)
#
# Installs the same tools, at the same versions, as Urmi's laptop: Docker (Docker's own apt
# repository), k3d 5.9.0 (k3s 1.35.5), kubectl 1.36.1, the Linkerd CLI edge-26.11.1, sops 3.13.3,
# age 1.3.2, Python 3.11 (deadsnakes; Ubuntu 24.04 ships 3.12), tmux and rsync. Every downloaded
# binary is checked against a pinned SHA-256 before it is installed (the values are GitHub's or
# the Kubernetes project's own, read on 10 Nov 2026). Then it makes the VM's own age key: its
# public half goes into .sops.yaml on the laptop (docs/vm.md, step 4); the private half never
# leaves the VM. Safe to run again.
set -euo pipefail
[ "$(uname -m)" = x86_64 ] || { echo "expected an x86_64 VM" >&2; exit 1; }

BIN=/usr/local/bin
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

fetch() {   # fetch <url> <sha256> <file>: download, check, or stop
  curl -fsSL --retry 3 -o "$TMP/$3" "$1"
  echo "$2  $TMP/$3" | sha256sum -c --quiet - || { echo "SHA-256 mismatch for $1" >&2; exit 1; }
}

echo "1/6 system packages"
sudo apt-get update -q
sudo apt-get install -yq ca-certificates curl gnupg tmux rsync git jq software-properties-common

echo "2/6 Docker, from Docker's own repository"
if ! command -v docker >/dev/null; then
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor --yes \
    -o /etc/apt/keyrings/docker.gpg
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update -q
  sudo apt-get install -yq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
sudo usermod -aG docker "$USER"   # takes effect at the next login

echo "3/6 k3d, kubectl, Linkerd, sops, age (pinned, checked)"
fetch https://github.com/k3d-io/k3d/releases/download/v5.9.0/k3d-linux-amd64 \
  06d8f25bc3a971c4eb29e0ff08429b180402db0f4dec838c9eac427e296800a0 k3d
fetch https://dl.k8s.io/release/v1.36.1/bin/linux/amd64/kubectl \
  629d3f410e09bf49b64ae7079f7f0bda1191efed311f7d37fdbab0ad5b0ec2b7 kubectl
fetch https://github.com/linkerd/linkerd2/releases/download/edge-26.11.1/linkerd2-cli-edge-26.11.1-linux-amd64 \
  f69701bec37964905d1cb06097e201ddf71e136824c3817f9a94a405fcbb6a07 linkerd
fetch https://github.com/getsops/sops/releases/download/v3.13.3/sops-v3.13.3.linux.amd64 \
  e5bec3346a873ae91d871550f3e698c1aad962aff462a080e40f25fde17fef6b sops
fetch https://github.com/FiloSottile/age/releases/download/v1.3.2/age-v1.3.2-linux-amd64.tar.gz \
  cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10 age.tar.gz
tar -xzf "$TMP/age.tar.gz" -C "$TMP"
for tool in k3d kubectl linkerd sops; do sudo install -m 0755 "$TMP/$tool" "$BIN/$tool"; done
sudo install -m 0755 "$TMP/age/age" "$TMP/age/age-keygen" "$BIN/"

echo "4/6 Python 3.11"
if ! command -v python3.11 >/dev/null; then
  sudo add-apt-repository -y ppa:deadsnakes/ppa
  sudo apt-get update -q
  sudo apt-get install -yq python3.11 python3.11-venv python3.11-dev
fi

echo "5/6 kernel limits for many pods and for Cassandra"
# k3d runs every node in Docker, and each pod watches files: the default inotify limits run out.
# Cassandra maps many files into memory.
sudo tee /etc/sysctl.d/90-fraud.conf >/dev/null <<'SYSCTL'
fs.inotify.max_user_instances = 1024
fs.inotify.max_user_watches = 1048576
vm.max_map_count = 1048575
SYSCTL
sudo sysctl --system >/dev/null

echo "6/6 the VM's own age key (its private half stays here)"
KEY="$HOME/.config/sops/age/keys.txt"
if [ ! -f "$KEY" ]; then
  mkdir -p "$(dirname "$KEY")"
  (umask 077; age-keygen -o "$KEY" 2>/dev/null)
fi
chmod 600 "$KEY"

echo
docker --version; docker compose version; k3d version | head -1; kubectl version --client | head -1
linkerd version --client --short; sops --version | head -1; age --version; python3.11 --version
echo
echo "The VM's age PUBLIC key (add it to .sops.yaml on the laptop: docs/vm.md, step 4):"
grep -o 'age1[0-9a-z]*' "$KEY"
echo
echo "Done. Log out and back in once, so Docker works without sudo."
