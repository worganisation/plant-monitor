#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd "$(dirname "$0")/.." && pwd -P)"
service_user="$(id -un)"
if [[ "$repo_dir" == *[!A-Za-z0-9_./-]* || "$service_user" == *[!A-Za-z0-9_.-]* ]]; then
    echo "Unsupported path or service user" >&2
    exit 1
fi
rendered="$(mktemp)"
trap 'rm -f "$rendered"' EXIT
sed -e "s|@SERVICE_USER@|$service_user|g" -e "s|@REPO_DIR@|$repo_dir|g" \
    "$repo_dir/service/plant-monitor.service" > "$rendered"
sudo install -m 0644 "$rendered" /etc/systemd/system/plant-monitor.service
sudo systemctl daemon-reload
