#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KUBECTL="${KUBECTL:-kubectl}"
SYSTEM_NAMESPACE="${SYSTEM_NAMESPACE:-thrive-scale}"
TARGET_NAMESPACE="${TARGET_NAMESPACE:-sock-shop}"
FRONTEND_SERVICE_TYPE="${FRONTEND_SERVICE_TYPE:-LoadBalancer}"
AGGREGATOR_SERVICE_TYPE="${AGGREGATOR_SERVICE_TYPE:-ClusterIP}"
IMAGE_PULL_POLICY="${IMAGE_PULL_POLICY:-IfNotPresent}"
AGGREGATOR_IMAGE="${AGGREGATOR_IMAGE:-loshans/aggregator:v1}"
CONTROLLER_IMAGE="${CONTROLLER_IMAGE:-loshans/controller:v1}"
FRONTEND_IMAGE="${FRONTEND_IMAGE:-loshans/frontend:v1}"
AGENT_IMAGE="${AGENT_IMAGE:-loshans/bpf-agent:v1}"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

apply_base() {
  "$KUBECTL" create namespace "$SYSTEM_NAMESPACE" --dry-run=client -o yaml | "$KUBECTL" apply -f -
  "$KUBECTL" apply -f "$ROOT_DIR/deploy/00-setup/crd-definition.yaml"
  "$KUBECTL" apply -f "$ROOT_DIR/deploy/00-setup/rbac.yaml"
  "$KUBECTL" apply -f "$ROOT_DIR/deploy/01-system/redis.yaml"
  "$KUBECTL" apply -f "$ROOT_DIR/deploy/01-system/aggregator.yaml"
  "$KUBECTL" apply -f "$ROOT_DIR/deploy/01-system/controller.yaml"
  "$KUBECTL" apply -f "$ROOT_DIR/deploy/01-system/agent.yaml"
  "$KUBECTL" apply -f "$ROOT_DIR/deploy/01-system/frontend.yaml"
}

patch_images_and_targets() {
  "$KUBECTL" -n "$SYSTEM_NAMESPACE" set image deploy/aggregator aggregator="$AGGREGATOR_IMAGE" >/dev/null
  "$KUBECTL" -n "$SYSTEM_NAMESPACE" set image deploy/custom-autoscaler controller="$CONTROLLER_IMAGE" >/dev/null
  "$KUBECTL" -n "$SYSTEM_NAMESPACE" set image deploy/frontend frontend="$FRONTEND_IMAGE" >/dev/null
  "$KUBECTL" -n "$SYSTEM_NAMESPACE" set image ds/bpf-agent bpf-agent="$AGENT_IMAGE" >/dev/null

  "$KUBECTL" -n "$SYSTEM_NAMESPACE" set env deploy/aggregator \
    TARGET_NAMESPACE="$TARGET_NAMESPACE" \
    AGENT_NAMESPACE="$SYSTEM_NAMESPACE" >/dev/null

  "$KUBECTL" -n "$SYSTEM_NAMESPACE" set env deploy/custom-autoscaler \
    TARGET_NAMESPACE="$TARGET_NAMESPACE" >/dev/null

  for workload in deploy/aggregator deploy/custom-autoscaler deploy/frontend ds/bpf-agent; do
    "$KUBECTL" -n "$SYSTEM_NAMESPACE" patch "$workload" --type=json \
      -p='[{"op":"replace","path":"/spec/template/spec/containers/0/imagePullPolicy","value":"'"${IMAGE_PULL_POLICY}"'"}]' >/dev/null
  done
}

patch_services() {
  "$KUBECTL" -n "$SYSTEM_NAMESPACE" patch svc/aggregator --type=merge -p "$(cat <<EOF
{
  "spec": {
    "type": "${AGGREGATOR_SERVICE_TYPE}",
    "ports": [
      {
        "port": 8000,
        "targetPort": 8000,
        "protocol": "TCP"
      }
    ]
  }
}
EOF
)" >/dev/null

  "$KUBECTL" -n "$SYSTEM_NAMESPACE" patch svc/frontend --type=merge -p "$(cat <<EOF
{
  "spec": {
    "type": "${FRONTEND_SERVICE_TYPE}",
    "ports": [
      {
        "port": 80,
        "targetPort": 80,
        "protocol": "TCP"
      }
    ]
  }
}
EOF
)" >/dev/null
}

restart_and_wait() {
  "$KUBECTL" -n "$SYSTEM_NAMESPACE" rollout restart deploy/redis
  "$KUBECTL" -n "$SYSTEM_NAMESPACE" rollout restart deploy/aggregator
  "$KUBECTL" -n "$SYSTEM_NAMESPACE" rollout restart deploy/custom-autoscaler
  "$KUBECTL" -n "$SYSTEM_NAMESPACE" rollout restart deploy/frontend
  "$KUBECTL" -n "$SYSTEM_NAMESPACE" rollout restart ds/bpf-agent

  "$KUBECTL" -n "$SYSTEM_NAMESPACE" rollout status deploy/redis --timeout=300s
  "$KUBECTL" -n "$SYSTEM_NAMESPACE" rollout status deploy/aggregator --timeout=300s
  "$KUBECTL" -n "$SYSTEM_NAMESPACE" rollout status deploy/custom-autoscaler --timeout=300s
  "$KUBECTL" -n "$SYSTEM_NAMESPACE" rollout status deploy/frontend --timeout=300s
  "$KUBECTL" -n "$SYSTEM_NAMESPACE" rollout status ds/bpf-agent --timeout=300s
}

enforce_runtime_patches() {
  "$KUBECTL" -n "$SYSTEM_NAMESPACE" patch deploy/frontend --type=json \
    -p='[{"op":"replace","path":"/spec/template/spec/containers/0/imagePullPolicy","value":"'"${IMAGE_PULL_POLICY}"'"}]' >/dev/null
  "$KUBECTL" -n "$SYSTEM_NAMESPACE" patch ds/bpf-agent --type=json \
    -p='[{"op":"replace","path":"/spec/template/spec/containers/0/imagePullPolicy","value":"'"${IMAGE_PULL_POLICY}"'"}]' >/dev/null
  "$KUBECTL" -n "$SYSTEM_NAMESPACE" patch svc/frontend --type=merge -p "$(cat <<EOF
{
  "spec": {
    "type": "${FRONTEND_SERVICE_TYPE}",
    "ports": [
      {
        "port": 80,
        "targetPort": 80,
        "protocol": "TCP"
      }
    ]
  }
}
EOF
)" >/dev/null
}

show_summary() {
  echo
  echo "[ok] ThriveScale deployed in namespace: $SYSTEM_NAMESPACE"
  echo "[ok] Target application namespace: $TARGET_NAMESPACE"
  echo "[ok] Aggregator image: $AGGREGATOR_IMAGE"
  echo "[ok] Controller image: $CONTROLLER_IMAGE"
  echo "[ok] Frontend image: $FRONTEND_IMAGE"
  echo "[ok] Agent image: $AGENT_IMAGE"
  "$KUBECTL" get deploy,ds,pods,svc -n "$SYSTEM_NAMESPACE"
}

main() {
  require_cmd "$KUBECTL"
  apply_base
  patch_images_and_targets
  patch_services
  enforce_runtime_patches
  restart_and_wait
  show_summary
}

main "$@"
