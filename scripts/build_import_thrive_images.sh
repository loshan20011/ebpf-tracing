#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

K3S_BIN="${K3S_BIN:-k3s}"
IMPORT_CMD="${IMPORT_CMD:-sudo $K3S_BIN ctr images import -}"

declare -A IMAGES=(
  ["src/aggregator"]="loshans/aggregator:v1"
  ["src/agent"]="loshans/bpf-agent:v1"
  ["src/controller"]="loshans/controller:v1"
  ["src/frontend"]="loshans/frontend:v1"
)

for context in "${!IMAGES[@]}"; do
  image="${IMAGES[$context]}"
  echo "[build] $image from $context"
  docker build --no-cache -t "$image" "$context"
  echo "[import] $image into k3s"
  docker save "$image" | eval "$IMPORT_CMD" >/dev/null
done

echo "[verify] imported images"
for image in "${IMAGES[@]}"; do
  sudo "$K3S_BIN" ctr images ls | grep -F "$image" >/dev/null
  echo "  ok $image"
done

echo "[done] ThriveScale images are built and imported into k3s"
