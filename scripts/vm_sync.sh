#!/usr/bin/env bash
# Laptop -> measurement VM: the committed code, the data, the model and this cluster's keys.
# Design: docs/vm.md
#
#   scripts/vm_sync.sh fraud-vm        (an SSH host from ~/.ssh/config; run again after a commit)
#
# Sends:
#   the code at HEAD (git archive: only what is committed, so the VM runs a known commit; the
#     repo is private, so the VM holds no GitHub credentials)
#   data/processed/ (the IEEE-CIS splits) and models/ (the pinned model)
#   keys/ (this cluster's JWT key and client secrets: made by `make keys`, used only by it)
#   promotion-keys/public.pem: the VM verifies promotions; they are signed on the laptop
# Never sends: promotion-keys/private.pem, your age key, .venv, results under data/.
set -euo pipefail
cd "$(dirname "$0")/.."
HOST=${1:?usage: scripts/vm_sync.sh <ssh host>}
DEST=fraud   # ~/fraud on the VM

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "Uncommitted changes: commit first, so the VM runs a known commit" >&2
  exit 1
fi
COMMIT=$(git rev-parse --short HEAD)
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

echo "1/4 code at $COMMIT"
git archive --format=tar HEAD | tar -x -C "$STAGE"
echo "$COMMIT" > "$STAGE/COMMIT"
ssh "$HOST" "mkdir -p ~/$DEST/data/processed ~/$DEST/models ~/$DEST/keys ~/$DEST/promotion-keys"
# --delete removes files deleted from the repo; what isn't code (data, models, keys, the venv,
# results) is excluded, so it is neither sent here nor deleted there
rsync -az --delete --exclude='/.venv/' --exclude='/data/' --exclude='/models/' \
  --exclude='/keys/' --exclude='/promotion-keys/' --exclude='/deploy/kong/kong.yml' \
  "$STAGE/" "$HOST:$DEST/"

echo "2/4 data and the model"
rsync -az data/processed/ "$HOST:$DEST/data/processed/"
rsync -az --delete models/ "$HOST:$DEST/models/"

echo "3/4 keys: this cluster's own, and the promotion PUBLIC key only"
rsync -az keys/ "$HOST:$DEST/keys/"
rsync -az promotion-keys/public.pem "$HOST:$DEST/promotion-keys/public.pem"
if ssh "$HOST" "test -e ~/$DEST/promotion-keys/private.pem"; then
  echo "A promotion PRIVATE key is on the VM: delete it (ssh $HOST rm ~/$DEST/promotion-keys/private.pem)" >&2
  exit 1
fi

echo "4/4 the Python environment on the VM"
ssh "$HOST" "cd ~/$DEST && { test -d .venv || python3.11 -m venv .venv; } && .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q -r requirements-dev.txt"
echo "VM now has commit $COMMIT in ~/$DEST"
