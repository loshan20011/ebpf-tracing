#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

APP_NS="${APP_NS:-sock-shop}"
CONTROL_NS="${CONTROL_NS:-thrive-scale}"
KUBECTL="${KUBECTL:-kubectl}"

ensure_ns() {
  $KUBECTL get ns "$1" >/dev/null 2>&1 || $KUBECTL create ns "$1" >/dev/null
}

wait_for_control_plane() {
  $KUBECTL rollout status deploy/redis -n "$CONTROL_NS" --timeout=240s
  $KUBECTL rollout status deploy/aggregator -n "$CONTROL_NS" --timeout=240s
  $KUBECTL rollout status deploy/frontend -n "$CONTROL_NS" --timeout=240s
  $KUBECTL rollout status deploy/custom-autoscaler -n "$CONTROL_NS" --timeout=240s
  $KUBECTL rollout status daemonset/bpf-agent --timeout=240s
}

show_state() {
  echo "[state] thrive-scale"
  $KUBECTL get pods -n "$CONTROL_NS"
  echo "[state] sock-shop"
  $KUBECTL get deploy -n "$APP_NS"
}

echo "[phase] build/import ThriveScale images"
bash scripts/build_import_thrive_images.sh

echo "[phase] ensure namespaces"
ensure_ns "$APP_NS"
ensure_ns "$CONTROL_NS"

echo "[phase] deploy thrive-scale RBAC and services"
$KUBECTL apply -f deploy/00-setup/rbac.yaml
$KUBECTL apply -f deploy/01-system/redis.yaml
$KUBECTL apply -f deploy/01-system/aggregator.yaml
$KUBECTL apply -f deploy/01-system/frontend.yaml
$KUBECTL apply -f deploy/01-system/controller.yaml
$KUBECTL apply -f deploy/01-system/agent.yaml

echo "[phase] wait for thrive-scale rollouts"
wait_for_control_plane

echo "[phase] redeploy sock-shop with paper-related fixes"
bash scripts/deploy_sock_shop_demo.sh
$KUBECTL patch deploy/front-end -n "$APP_NS" --type strategic --patch-file deploy/03-evaluation/front-end-stability-patch.yaml
$KUBECTL rollout status deploy/front-end -n "$APP_NS" --timeout=240s

echo "[phase] verify sock-shop paper fixes"
bash scripts/verify_sockshop_paper_fixes.sh

echo "[phase] warm reset control plane state"
$KUBECTL exec -n "$CONTROL_NS" deploy/redis -- redis-cli FLUSHALL >/dev/null || true
$KUBECTL rollout restart deploy/aggregator -n "$CONTROL_NS" >/dev/null
$KUBECTL rollout restart daemonset/bpf-agent >/dev/null
$KUBECTL rollout status deploy/aggregator -n "$CONTROL_NS" --timeout=240s
$KUBECTL rollout status daemonset/bpf-agent --timeout=240s

show_state
echo "[done] paper-like worldcup environment is ready"
