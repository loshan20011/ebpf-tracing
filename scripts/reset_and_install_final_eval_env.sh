#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

APP_NS="${APP_NS:-sock-shop}"
CONTROL_NS="${CONTROL_NS:-thrive-scale}"
KUBECTL="${KUBECTL:-kubectl}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-240s}"
SOCKSHOP_COMPONENT_SET="${SOCKSHOP_COMPONENT_SET:-all}"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

ensure_ns() {
  "$KUBECTL" get namespace "$1" >/dev/null 2>&1 || "$KUBECTL" create namespace "$1" >/dev/null
}

wipe_environment() {
  echo "[phase] wipe prior evaluation state"
  "$KUBECTL" delete namespace "$APP_NS" --ignore-not-found=true --wait=true || true
  "$KUBECTL" delete namespace "$CONTROL_NS" --ignore-not-found=true --wait=true || true
  "$KUBECTL" delete crd serviceslos.autoscaling.fyp.io --ignore-not-found=true || true
  "$KUBECTL" delete clusterrole aggregator-reader autoscaler-role bpf-agent-reader --ignore-not-found=true || true
  "$KUBECTL" delete clusterrolebinding aggregator-binding autoscaler-binding bpf-agent-reader-binding --ignore-not-found=true || true
}

build_images() {
  echo "[phase] build/import ThriveScale images"
  bash scripts/build_import_thrive_images.sh
}

deploy_control_plane() {
  echo "[phase] create namespaces"
  ensure_ns "$APP_NS"
  ensure_ns "$CONTROL_NS"

  echo "[phase] apply CRD, RBAC, and ThriveScale services"
  "$KUBECTL" apply -f deploy/00-setup/crd-definition.yaml
  "$KUBECTL" apply -f deploy/00-setup/rbac.yaml
  "$KUBECTL" apply -f deploy/01-system/redis.yaml
  "$KUBECTL" apply -f deploy/01-system/aggregator.yaml
  "$KUBECTL" apply -f deploy/01-system/frontend.yaml
  "$KUBECTL" apply -f deploy/01-system/controller.yaml
  "$KUBECTL" apply -f deploy/01-system/agent.yaml

  echo "[phase] wait for ThriveScale rollouts"
  "$KUBECTL" rollout status deploy/redis -n "$CONTROL_NS" --timeout="$WAIT_TIMEOUT"
  "$KUBECTL" rollout status deploy/aggregator -n "$CONTROL_NS" --timeout="$WAIT_TIMEOUT"
  "$KUBECTL" rollout status deploy/frontend -n "$CONTROL_NS" --timeout="$WAIT_TIMEOUT"
  "$KUBECTL" rollout status deploy/custom-autoscaler -n "$CONTROL_NS" --timeout="$WAIT_TIMEOUT"
  "$KUBECTL" rollout status daemonset/bpf-agent -n "$CONTROL_NS" --timeout="$WAIT_TIMEOUT"
}

deploy_sock_shop() {
  echo "[phase] deploy full Sock Shop"
  SOCKSHOP_COMPONENT_SET="$SOCKSHOP_COMPONENT_SET" bash scripts/deploy_sock_shop_demo.sh

  echo "[phase] apply Sock Shop stability patch"
  "$KUBECTL" patch deploy/front-end -n "$APP_NS" --type strategic --patch-file deploy/03-evaluation/front-end-stability-patch.yaml
  "$KUBECTL" rollout status deploy/front-end -n "$APP_NS" --timeout="$WAIT_TIMEOUT"

  echo "[phase] verify Sock Shop guardrails"
  bash scripts/verify_sockshop_paper_fixes.sh
}

reset_runtime_state() {
  echo "[phase] reset runtime state"
  "$KUBECTL" exec -n "$CONTROL_NS" deploy/redis -- redis-cli FLUSHALL >/dev/null || true
  "$KUBECTL" rollout restart deploy/aggregator -n "$CONTROL_NS" >/dev/null
  "$KUBECTL" rollout restart deploy/custom-autoscaler -n "$CONTROL_NS" >/dev/null
  "$KUBECTL" rollout restart daemonset/bpf-agent -n "$CONTROL_NS" >/dev/null
  "$KUBECTL" rollout status deploy/aggregator -n "$CONTROL_NS" --timeout="$WAIT_TIMEOUT"
  "$KUBECTL" rollout status deploy/custom-autoscaler -n "$CONTROL_NS" --timeout="$WAIT_TIMEOUT"
  "$KUBECTL" rollout status daemonset/bpf-agent -n "$CONTROL_NS" --timeout="$WAIT_TIMEOUT"
}

show_state() {
  echo "[state] thrive-scale pods"
  "$KUBECTL" get pods -n "$CONTROL_NS"
  echo "[state] sock-shop deployments"
  "$KUBECTL" get deploy -n "$APP_NS"
  echo "[state] sock-shop services"
  "$KUBECTL" get svc -n "$APP_NS"
}

main() {
  require_cmd bash
  require_cmd git
  require_cmd python3
  require_cmd "$KUBECTL"

  if [[ "$SOCKSHOP_COMPONENT_SET" != "all" ]]; then
    echo "SOCKSHOP_COMPONENT_SET must be 'all' for final evaluation bootstrap" >&2
    exit 1
  fi

  wipe_environment
  build_images
  deploy_control_plane
  deploy_sock_shop
  reset_runtime_state
  show_state

  echo "[done] final evaluation environment is reset and full Sock Shop is installed"
}

main "$@"
