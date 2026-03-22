#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

NAMESPACE="${NAMESPACE:-sock-shop}"
REPO_URL="${REPO_URL:-https://github.com/ocp-power-demos/sock-shop-demo.git}"
REPO_DIR="${REPO_DIR:-/tmp/sock-shop-demo}"
KUBECTL="${KUBECTL:-kubectl}"
MONGO_USER="${MONGO_USER:-root}"
MONGO_PASSWORD="${MONGO_PASSWORD:-admin}"
SESSION_DB_IMAGE="${SESSION_DB_IMAGE:-redis:7-alpine}"
FRONTEND_NODE_PORT="${FRONTEND_NODE_PORT:-30001}"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

clone_repo() {
  if [[ -d "$REPO_DIR/.git" ]]; then
    git -C "$REPO_DIR" fetch --depth 1 origin main >/dev/null 2>&1 || true
    git -C "$REPO_DIR" reset --hard origin/main >/dev/null 2>&1 || true
  else
    rm -rf "$REPO_DIR"
    git clone --depth 1 "$REPO_URL" "$REPO_DIR"
  fi
}

install_base_resources() {
  "$KUBECTL" delete namespace "$NAMESPACE" --ignore-not-found=true --wait=true || true
  "$KUBECTL" create namespace "$NAMESPACE"

  "$KUBECTL" -n "$NAMESPACE" create secret generic mongodb-creds \
    --from-literal=username="$MONGO_USER" \
    --from-literal=password="$MONGO_PASSWORD"

  for file in "$REPO_DIR"/manifests/overlays/single/*.yaml; do
    base="$(basename "$file")"
    case "$base" in
      env.secret|kustomization.yaml|29-route-front-end.yaml)
        continue
        ;;
    esac
    "$KUBECTL" apply -n "$NAMESPACE" -f "$file"
  done
}

normalize_pvcs() {
  "$KUBECTL" delete pvc carts-db-temp-pvc orders-db-temp-pvc user-db-temp-pvc -n "$NAMESPACE" --ignore-not-found=true || true
  cat <<EOF | "$KUBECTL" apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: carts-db-temp-pvc
  namespace: ${NAMESPACE}
spec:
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: 1Gi
  storageClassName: local-path
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: orders-db-temp-pvc
  namespace: ${NAMESPACE}
spec:
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: 1Gi
  storageClassName: local-path
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: user-db-temp-pvc
  namespace: ${NAMESPACE}
spec:
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: 1Gi
  storageClassName: local-path
EOF
}

patch_deployments() {
  local deploys=(
    carts carts-db catalogue catalogue-db front-end orders orders-db
    payment queue-master rabbitmq session-db shipping user user-db
  )

  for d in "${deploys[@]}"; do
    "$KUBECTL" patch deploy "$d" -n "$NAMESPACE" --type=json \
      -p='[{"op":"remove","path":"/spec/template/spec/nodeSelector"}]' >/dev/null 2>&1 || true
  done

  for d in carts catalogue front-end orders payment queue-master shipping user rabbitmq; do
    "$KUBECTL" patch deploy "$d" -n "$NAMESPACE" --type=json \
      -p='[{"op":"remove","path":"/spec/template/spec/containers/0/securityContext/runAsNonRoot"}]' >/dev/null 2>&1 || true
    "$KUBECTL" patch deploy "$d" -n "$NAMESPACE" --type=json \
      -p='[{"op":"remove","path":"/spec/template/spec/containers/1/securityContext/runAsNonRoot"}]' >/dev/null 2>&1 || true
  done

  "$KUBECTL" set image deploy/session-db -n "$NAMESPACE" "session-db=${SESSION_DB_IMAGE}" >/dev/null
  "$KUBECTL" patch deploy/session-db -n "$NAMESPACE" --type=json \
    -p='[{"op":"replace","path":"/spec/template/spec/containers/0/securityContext","value":{"runAsUser":0}}]' >/dev/null
  "$KUBECTL" patch deploy/catalogue-db -n "$NAMESPACE" --type=json \
    -p='[{"op":"replace","path":"/spec/template/spec/containers/0/securityContext","value":{"runAsUser":0}}]' >/dev/null
}

expose_frontend() {
  "$KUBECTL" patch svc front-end -n "$NAMESPACE" --type=merge \
    -p "{\"spec\":{\"type\":\"NodePort\",\"ports\":[{\"port\":80,\"targetPort\":8079,\"protocol\":\"TCP\",\"nodePort\":${FRONTEND_NODE_PORT}}]}}" >/dev/null
}

restart_and_wait() {
  local deploys=(
    carts carts-db catalogue catalogue-db front-end orders orders-db
    payment queue-master rabbitmq session-db shipping user user-db
  )

  for d in "${deploys[@]}"; do
    "$KUBECTL" scale deploy "$d" -n "$NAMESPACE" --replicas=1 >/dev/null 2>&1 || true
  done

  for d in "${deploys[@]}"; do
    "$KUBECTL" rollout restart deploy "$d" -n "$NAMESPACE" >/dev/null 2>&1 || true
  done

  for d in "${deploys[@]}"; do
    "$KUBECTL" rollout status deploy "$d" -n "$NAMESPACE" --timeout=300s
  done
}

show_summary() {
  echo
  echo "[ok] Sock Shop deployed in namespace: $NAMESPACE"
  "$KUBECTL" get deploy -n "$NAMESPACE"
  echo "---"
  "$KUBECTL" get pods -n "$NAMESPACE"
  echo "---"
  "$KUBECTL" get svc -n "$NAMESPACE"
}

main() {
  require_cmd git
  require_cmd "$KUBECTL"

  clone_repo
  install_base_resources
  normalize_pvcs
  patch_deployments
  expose_frontend
  restart_and_wait
  show_summary
}

main "$@"
