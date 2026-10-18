#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
cd "$(dirname "$0")/.."
tag="${1:?Usage: deploy.sh RELEASE_TAG}"
[[ "$tag" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || exit 1
if [[ -n "$(git status --porcelain)" ]]; then
    echo "Working tree is dirty; refusing deployment" >&2
    exit 1
fi
git fetch origin "refs/tags/$tag:refs/tags/$tag"
sudo systemctl stop plant-monitor.service
git checkout --detach "$tag"
uv sync --frozen --no-dev
bash service/install.sh
sudo systemctl enable --now plant-monitor.service
sleep 5
systemctl is-active --quiet plant-monitor.service
