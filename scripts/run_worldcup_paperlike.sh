#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

APP_NS="${APP_NS:-sock-shop}"
CONTROL_NS="${CONTROL_NS:-thrive-scale}"
SLO_FILE="${SLO_FILE:-deploy/03-evaluation/sockshop-slos.calibrated.yaml}"
HPA_FILE="${HPA_FILE:-deploy/03-evaluation/hpa-sockshop-paperlike.yaml}"
MIX_FILE="${MIX_FILE:-deploy/03-evaluation/workloads/worldcup98-day75-peak.yaml}"
GUARDRAILS_FILE="${GUARDRAILS_FILE:-deploy/03-evaluation/sockshop-guardrails.yaml}"
FRONTEND_PATCH_FILE="${FRONTEND_PATCH_FILE:-deploy/03-evaluation/front-end-stability-patch.yaml}"
CONTROLLER_FILE="${CONTROLLER_FILE:-deploy/01-system/controller.yaml}"
RUNS="${RUNS:-1}"
HPA_RUNS="${HPA_RUNS:-$RUNS}"
THRIVE_RUNS="${THRIVE_RUNS:-$RUNS}"
EVAL_MODE="${EVAL_MODE:-both}"
WARMUP_SECONDS="${WARMUP_SECONDS:-30}"
SAMPLE_INTERVAL="${SAMPLE_INTERVAL:-2}"
TIMEOUT="${TIMEOUT:-8}"
SETTLE_SECONDS="${SETTLE_SECONDS:-20}"
CLEAN_RESULTS="${CLEAN_RESULTS:-1}"
RESULT_DIR="${RESULT_DIR:-results/worldcup-paperlike}"
ANALYSIS_DIR="${ANALYSIS_DIR:-results/analysis/worldcup-paperlike}"
KUBECTL="${KUBECTL:-kubectl}"
PYTHON="${PYTHON:-python3}"
CONTROL_TARGET="${CONTROL_TARGET:-front-end}"
CLIENT_SLO_MS="${CLIENT_SLO_MS:-41}"
BUILD_AUTOSCALER="${BUILD_AUTOSCALER:-1}"
AUTOSCALER_IMAGE="${AUTOSCALER_IMAGE:-loshans/controller:v1}"
IMAGE_BOOTSTRAP_SCRIPT="${IMAGE_BOOTSTRAP_SCRIPT:-scripts/build_import_thrive_images.sh}"

read -r -a SERVICES <<< "${SERVICES:-front-end catalogue carts orders catalogue-db shipping}"
read -r -a SCALER_SERVICES <<< "${SCALER_SERVICES:-front-end catalogue carts orders}"

mkdir -p "$RESULT_DIR" "$ANALYSIS_DIR"

normalize_eval_mode() {
  local mode
  mode="$(printf '%s' "$EVAL_MODE" | tr '[:upper:]' '[:lower:]')"
  case "$mode" in
    both)
      ;;
    hpa)
      HPA_RUNS="${HPA_RUNS:-$RUNS}"
      THRIVE_RUNS=0
      ;;
    thrivescale)
      HPA_RUNS=0
      THRIVE_RUNS="${THRIVE_RUNS:-$RUNS}"
      ;;
    *)
      echo "[fail] invalid EVAL_MODE=$EVAL_MODE (expected: both|hpa|thrivescale)" >&2
      exit 1
      ;;
  esac
  EVAL_MODE="$mode"
}

clean_result_noise() {
  if [[ "$CLEAN_RESULTS" != "1" ]]; then
    return
  fi
  rm -f "$RESULT_DIR"/results_worldcup_paperlike_*.csv
  rm -f "$RESULT_DIR"/results_worldcup_paperlike_*.breaches.csv
  rm -f "$ANALYSIS_DIR"/worldcup_paperlike_summary.md
}

NODE_IP="$($KUBECTL get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="ExternalIP")].address}' 2>/dev/null || true)"
if [[ -z "$NODE_IP" ]]; then
  NODE_IP="$($KUBECTL get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')"
fi
FRONTEND_NODE_PORT="$($KUBECTL get svc front-end -n "$APP_NS" -o jsonpath='{.spec.ports[0].nodePort}')"
AGG_NODE_PORT="$($KUBECTL get svc aggregator -n "$CONTROL_NS" -o jsonpath='{.spec.ports[0].nodePort}')"
TARGET_URL="${TARGET_URL:-http://${NODE_IP}:${FRONTEND_NODE_PORT}}"
AGGREGATOR_URL="${AGGREGATOR_URL:-http://${NODE_IP}:${AGG_NODE_PORT}}"
DURATION="${DURATION:-$(awk '/end_s:/ { if ($2+0 > max) max = $2+0 } END { print int(max+0) }' "$MIX_FILE")}"

build_and_import_autoscaler() {
  if [[ "$BUILD_AUTOSCALER" != "1" ]]; then
    echo "[skip] BUILD_AUTOSCALER=$BUILD_AUTOSCALER"
    return
  fi
  echo "[phase] build/import ThriveScale images into k3s"
  bash "$IMAGE_BOOTSTRAP_SCRIPT"
}

ensure_prereqs() {
  echo "[phase] apply paper-like guardrails and probe stability patch"
  $KUBECTL apply -f "$GUARDRAILS_FILE" >/dev/null
  $KUBECTL patch deploy/front-end -n "$APP_NS" --type strategic --patch-file "$FRONTEND_PATCH_FILE" >/dev/null
  $KUBECTL rollout status deploy/front-end -n "$APP_NS" --timeout=240s >/dev/null
}

fix_sockshop_service_ports() {
  # Some demo states leave service targetPorts at 8080 while pods serve on 80.
  # Normalize these mappings on every reset so front-end dependencies stay healthy.
  $KUBECTL apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Service
metadata:
  name: catalogue
  namespace: ${APP_NS}
spec:
  selector:
    name: catalogue
  ports:
  - port: 80
    targetPort: 80
    protocol: TCP
---
apiVersion: v1
kind: Service
metadata:
  name: carts
  namespace: ${APP_NS}
spec:
  selector:
    name: carts
  ports:
  - port: 80
    targetPort: 80
    protocol: TCP
---
apiVersion: v1
kind: Service
metadata:
  name: orders
  namespace: ${APP_NS}
spec:
  selector:
    name: orders
  ports:
  - port: 80
    targetPort: 80
    protocol: TCP
---
apiVersion: v1
kind: Service
metadata:
  name: shipping
  namespace: ${APP_NS}
spec:
  selector:
    name: shipping
  ports:
  - port: 80
    targetPort: 80
    protocol: TCP
---
apiVersion: v1
kind: Service
metadata:
  name: payment
  namespace: ${APP_NS}
spec:
  selector:
    name: payment
  ports:
  - port: 80
    targetPort: 80
    protocol: TCP
---
apiVersion: v1
kind: Service
metadata:
  name: user
  namespace: ${APP_NS}
spec:
  selector:
    name: user
  ports:
  - port: 80
    targetPort: 80
    protocol: TCP
EOF
}

reset_sockshop_baseline() {
  echo "[phase] reset sock-shop baseline"
  $KUBECTL delete hpa --all -n "$APP_NS" --ignore-not-found >/dev/null 2>&1 || true
  $KUBECTL delete hpa front-end-hpa catalogue-hpa carts-hpa orders-hpa -n default --ignore-not-found >/dev/null 2>&1 || true
  $KUBECTL delete serviceslo --all -n "$APP_NS" --ignore-not-found >/dev/null 2>&1 || true
  for svc in front-end catalogue carts orders catalogue-db carts-db orders-db user user-db payment queue-master rabbitmq session-db shipping; do
    $KUBECTL scale deploy/"$svc" -n "$APP_NS" --replicas=1 >/dev/null 2>&1 || true
  done
  for svc in "${SERVICES[@]}"; do
    $KUBECTL rollout status deploy/"$svc" -n "$APP_NS" --timeout=240s >/dev/null
  done
  fix_sockshop_service_ports
  sleep "$SETTLE_SECONDS"
}

reset_control_plane() {
  echo "[phase] reset control plane"
  $KUBECTL exec -n "$CONTROL_NS" deploy/redis -- redis-cli FLUSHALL >/dev/null
  $KUBECTL rollout restart deploy/aggregator -n "$CONTROL_NS" >/dev/null
  $KUBECTL rollout restart daemonset/bpf-agent -n "$CONTROL_NS" >/dev/null
  $KUBECTL rollout status deploy/aggregator -n "$CONTROL_NS" --timeout=240s >/dev/null
  $KUBECTL rollout status daemonset/bpf-agent -n "$CONTROL_NS" --timeout=240s >/dev/null
}

verify_guardrails() {
  echo "[check] verifying namespace guardrails"
  $KUBECTL get limitrange sock-shop-defaults -n "$APP_NS" >/dev/null
  $KUBECTL get resourcequota sock-shop-quota -n "$APP_NS" >/dev/null
}

verify_capacity_headroom() {
  echo "[check] verifying front-end can exceed small scale safely"
  $KUBECTL scale deploy/front-end -n "$APP_NS" --replicas=6 >/dev/null
  $KUBECTL rollout status deploy/front-end -n "$APP_NS" --timeout=240s >/dev/null
  state="$($KUBECTL get deploy front-end -n "$APP_NS" -o jsonpath='{.status.readyReplicas}')"
  if [[ "$state" != "6" ]]; then
    echo "[fail] front-end readiness headroom check failed (ready=$state expected=6)" >&2
    exit 1
  fi
  $KUBECTL scale deploy/front-end -n "$APP_NS" --replicas=1 >/dev/null
  $KUBECTL rollout status deploy/front-end -n "$APP_NS" --timeout=240s >/dev/null
}

verify_clean_state() {
  echo "[check] verifying baseline deployments"
  for svc in "${SCALER_SERVICES[@]}"; do
    state="$($KUBECTL get deploy "$svc" -n "$APP_NS" -o jsonpath='{.spec.replicas}:{.status.readyReplicas}')"
    echo "[check] $svc -> $state"
  done
  echo "[check] control plane pods"
  $KUBECTL get pods -n "$CONTROL_NS"
}

prepare_hpa_mode() {
  echo "[phase] prepare HPA mode"
  $KUBECTL scale deploy/custom-autoscaler -n "$CONTROL_NS" --replicas=0 >/dev/null
  $KUBECTL wait --for=jsonpath='{.status.availableReplicas}'= --timeout=90s deploy/custom-autoscaler -n "$CONTROL_NS" >/dev/null 2>&1 || true
  $KUBECTL apply -f "$SLO_FILE" >/dev/null
  $KUBECTL apply -f "$HPA_FILE" >/dev/null
  sleep 15
  $PYTHON scripts/benchmark_preflight.py \
    --app-namespace "$APP_NS" \
    --control-namespace "$CONTROL_NS" \
    --aggregator-url "$AGGREGATOR_URL" \
    --services "${SCALER_SERVICES[@]}" \
    --expected-replicas 1 \
    --mode hpa
}

prepare_thrivescale_mode() {
  echo "[phase] prepare ThriveScale mode"
  build_and_import_autoscaler
  $KUBECTL delete hpa --all -n "$APP_NS" --ignore-not-found >/dev/null 2>&1 || true
  $KUBECTL apply -f "$CONTROLLER_FILE" >/dev/null
  $KUBECTL scale deploy/custom-autoscaler -n "$CONTROL_NS" --replicas=1 >/dev/null
  $KUBECTL rollout status deploy/custom-autoscaler -n "$CONTROL_NS" --timeout=240s >/dev/null
  $KUBECTL apply -f "$SLO_FILE" >/dev/null
  $PYTHON scripts/benchmark_preflight.py \
    --app-namespace "$APP_NS" \
    --control-namespace "$CONTROL_NS" \
    --aggregator-url "$AGGREGATOR_URL" \
    --services "${SCALER_SERVICES[@]}" \
    --expected-replicas 1 \
    --mode thrivescale
}

run_one() {
  local scaler="$1"
  local run_id="$2"
  local csv="$RESULT_DIR/results_worldcup_paperlike_${scaler}_run${run_id}.csv"
  echo "[run] scaler=$scaler run=$run_id csv=$csv"
  $PYTHON src/load-generator/eval_harness.py \
    --url "$TARGET_URL" \
    --deployment front-end \
    --namespace "$APP_NS" \
    --profile sockshop \
    --mix-file "$MIX_FILE" \
    --duration "$DURATION" \
    --sample-interval "$SAMPLE_INTERVAL" \
    --timeout "$TIMEOUT" \
    --warmup-seconds "$WARMUP_SECONDS" \
    --slo-latency-ms "$CLIENT_SLO_MS" \
    --aggregator-url "$AGGREGATOR_URL" \
    --control-target "$CONTROL_TARGET" \
    --csv "$csv"
}

summarize() {
  $PYTHON scripts/analyze_eval.py \
    --glob "$RESULT_DIR/results_worldcup_paperlike_*.csv" \
    --scenario worldcup \
    --default-slo-ms "$CLIENT_SLO_MS" \
    --slo-file "$SLO_FILE" \
    --namespace "$APP_NS" \
    --slo-target "$CONTROL_TARGET" \
    --warmup-seconds "$WARMUP_SECONDS" \
    --plot \
    --markdown-out "$ANALYSIS_DIR/worldcup_paperlike_summary.md"
}

echo "[info] target_url=$TARGET_URL"
echo "[info] aggregator_url=$AGGREGATOR_URL"
echo "[info] mix_file=$MIX_FILE"
echo "[info] hpa_file=$HPA_FILE"
echo "[info] slo_file=$SLO_FILE"
echo "[info] duration=$DURATION"
echo "[info] eval_mode=$EVAL_MODE"

normalize_eval_mode
echo "[info] hpa_runs=$HPA_RUNS thrive_runs=$THRIVE_RUNS"

clean_result_noise
ensure_prereqs
reset_sockshop_baseline
reset_control_plane
verify_guardrails
verify_capacity_headroom
verify_clean_state

if [[ "$HPA_RUNS" -gt 0 ]]; then
  prepare_hpa_mode
  for i in $(seq 1 "$HPA_RUNS"); do
    reset_sockshop_baseline
    reset_control_plane
    verify_guardrails
    verify_clean_state
    prepare_hpa_mode
    run_one "hpa" "$i"
  done
fi

if [[ "$THRIVE_RUNS" -gt 0 ]]; then
  reset_sockshop_baseline
  reset_control_plane
  verify_guardrails
  verify_clean_state

  prepare_thrivescale_mode
  for i in $(seq 1 "$THRIVE_RUNS"); do
    reset_sockshop_baseline
    reset_control_plane
    verify_guardrails
    verify_clean_state
    prepare_thrivescale_mode
    run_one "thrivescale" "$i"
  done
fi

summarize

echo "[done] paper-like worldcup comparison completed"
