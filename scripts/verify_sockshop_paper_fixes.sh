#!/usr/bin/env bash
set -euo pipefail

APP_NS="${APP_NS:-sock-shop}"
KUBECTL="${KUBECTL:-kubectl}"

pass=true

check() {
  local desc="$1"
  shift
  if "$@"; then
    echo "[ok] $desc"
  else
    echo "[fail] $desc"
    pass=false
  fi
}

probe_check() {
  local deploy="$1"
  local probe="$2"
  local value
  value="$($KUBECTL get deploy "$deploy" -n "$APP_NS" -o jsonpath="{.spec.template.spec.containers[0].${probe}.tcpSocket.port}" 2>/dev/null || true)"
  [[ -n "$value" ]]
}

resource_exists() {
  local kind="$1"
  local name="$2"
  $KUBECTL get "$kind" "$name" -n "$APP_NS" >/dev/null 2>&1
}

emptydir_check() {
  local deploy="$1"
  local mounts
  mounts="$($KUBECTL get deploy "$deploy" -n "$APP_NS" -o jsonpath='{range .spec.template.spec.volumes[*]}{.emptyDir}{"\n"}{end}' 2>/dev/null || true)"
  [[ -n "$mounts" ]]
}

check "LimitRange present" resource_exists limitrange sock-shop-defaults
check "ResourceQuota present" resource_exists resourcequota sock-shop-quota

check "carts startup probe configured" probe_check carts startupProbe
check "carts readiness probe configured" probe_check carts readinessProbe
check "carts liveness probe configured" probe_check carts livenessProbe

check "orders startup probe configured" probe_check orders startupProbe
check "orders readiness probe configured" probe_check orders readinessProbe
check "orders liveness probe configured" probe_check orders livenessProbe

check "shipping startup probe configured" probe_check shipping startupProbe
check "shipping readiness probe configured" probe_check shipping readinessProbe
check "shipping liveness probe configured" probe_check shipping livenessProbe

check "catalogue-db uses emptyDir-backed storage" emptydir_check catalogue-db
check "carts-db uses emptyDir-backed storage" emptydir_check carts-db
check "orders-db uses emptyDir-backed storage" emptydir_check orders-db
check "user-db uses emptyDir-backed storage" emptydir_check user-db
check "session-db uses emptyDir-backed storage" emptydir_check session-db

if [[ "$pass" == true ]]; then
  echo "[done] Sock Shop paper-fix checks passed"
else
  echo "[done] Sock Shop paper-fix checks failed"
  exit 1
fi
