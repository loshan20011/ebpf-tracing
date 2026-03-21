#!/usr/bin/env bash
set -euo pipefail

APP_NS="${APP_NS:-sock-shop}"
CONTROL_NS="${CONTROL_NS:-thrive-scale}"
SLO_FILE="${SLO_FILE:-deploy/02-demo-apps/my-slos.yaml}"
HPA_FILE="${HPA_FILE:-deploy/03-evaluation/hpa-demo.yaml}"
MIX_FILE="${MIX_FILE:-deploy/03-evaluation/workloads/demo-thrivescale-vs-hpa.yaml}"
RUNS="${RUNS:-2}"
DURATION="${DURATION:-220}"
WARMUP_SECONDS="${WARMUP_SECONDS:-20}"
SAMPLE_INTERVAL="${SAMPLE_INTERVAL:-2}"
TIMEOUT="${TIMEOUT:-8}"
RESULT_DIR="${RESULT_DIR:-results/demo-compare}"
ANALYSIS_DIR="${ANALYSIS_DIR:-results/analysis}"
KUBECTL="${KUBECTL:-kubectl}"
PYTHON="${PYTHON:-python3}"
CONTROL_TARGET="${CONTROL_TARGET:-svc-cpu}"
CLIENT_SLO_MS="${CLIENT_SLO_MS:-120}"
SETTLE_SECONDS="${SETTLE_SECONDS:-20}"

mkdir -p "$RESULT_DIR" "$ANALYSIS_DIR"

NODE_IP="$($KUBECTL get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="ExternalIP")].address}' 2>/dev/null || true)"
if [[ -z "$NODE_IP" ]]; then
  NODE_IP="$($KUBECTL get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')"
fi

GATEWAY_PORT="$($KUBECTL get svc gateway -n "$APP_NS" -o jsonpath='{.spec.ports[0].port}')"
GATEWAY_NODE_PORT="$($KUBECTL get svc gateway -n "$APP_NS" -o jsonpath='{.spec.ports[0].nodePort}')"
AGG_NODE_PORT="$($KUBECTL get svc aggregator -n "$CONTROL_NS" -o jsonpath='{.spec.ports[0].nodePort}')"
LB_HOST="$($KUBECTL get svc gateway -n "$APP_NS" -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
LB_IP="$($KUBECTL get svc gateway -n "$APP_NS" -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)"
if [[ -n "$LB_HOST" ]]; then
  GATEWAY_URL="${GATEWAY_URL:-http://${LB_HOST}:${GATEWAY_PORT}}"
elif [[ -n "$LB_IP" ]]; then
  GATEWAY_URL="${GATEWAY_URL:-http://${LB_IP}:${GATEWAY_PORT}}"
else
  GATEWAY_URL="${GATEWAY_URL:-http://${NODE_IP}:${GATEWAY_NODE_PORT}}"
fi
AGGREGATOR_URL="${AGGREGATOR_URL:-http://${NODE_IP}:${AGG_NODE_PORT}}"

SERVICES=(gateway svc-cpu svc-io svc-mem svc-net svc-chain svc-fanout)
SLO_TARGETS=(svc-cpu svc-io svc-mem svc-chain svc-fanout)

reset_demo_baseline() {
  echo "[phase] reset demo baseline"
  $KUBECTL scale deployment/traffic-gen -n "$APP_NS" --replicas=0 >/dev/null 2>&1 || true
  $KUBECTL delete hpa -n "$APP_NS" --all --ignore-not-found >/dev/null 2>&1 || true
  for svc in "${SERVICES[@]}"; do
    $KUBECTL scale deployment/"$svc" -n "$APP_NS" --replicas=1 >/dev/null 2>&1 || true
  done
  sleep "$SETTLE_SECONDS"
}

wait_control_plane_ready() {
  $KUBECTL rollout status -n "$CONTROL_NS" deploy/aggregator --timeout=180s
  $KUBECTL rollout status -n "$CONTROL_NS" deploy/custom-autoscaler --timeout=180s || true
}

reset_control_state() {
  curl -fsS -m 8 -X POST "$AGGREGATOR_URL/api/reset" >/dev/null 2>&1 || true
  $KUBECTL rollout restart -n "$CONTROL_NS" deploy/aggregator >/dev/null
  $KUBECTL rollout restart -n "$CONTROL_NS" deploy/custom-autoscaler >/dev/null || true
  wait_control_plane_ready
  sleep 8
}

verify_clean_state() {
  for svc in "${SERVICES[@]}"; do
    state="$($KUBECTL get deploy "$svc" -n "$APP_NS" -o jsonpath='{.spec.replicas}:{.status.readyReplicas}')"
    echo "[check] $svc -> $state"
  done
  $KUBECTL get hpa -n "$APP_NS" || true
  $KUBECTL get pods -n "$CONTROL_NS"
}

run_one() {
  local scaler="$1"
  local run_id="$2"
  local csv="$RESULT_DIR/results_demo_${scaler}_run${run_id}.csv"

  reset_demo_baseline
  reset_control_state

  $PYTHON scripts/benchmark_preflight.py \
    --app-namespace "$APP_NS" \
    --control-namespace "$CONTROL_NS" \
    --aggregator-url "$AGGREGATOR_URL" \
    --services "${SLO_TARGETS[@]}" \
    --expected-replicas 1 \
    --mode "$scaler"

  $PYTHON src/load-generator/eval_harness.py \
    --url "$GATEWAY_URL" \
    --deployment gateway \
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

echo "[info] gateway_url=$GATEWAY_URL"
echo "[info] aggregator_url=$AGGREGATOR_URL"
echo "[info] mix_file=$MIX_FILE"

reset_demo_baseline
verify_clean_state

echo "[phase] HPA baseline"
$KUBECTL scale deploy/custom-autoscaler -n "$CONTROL_NS" --replicas=0
$KUBECTL apply -n "$APP_NS" -f "$HPA_FILE"
sleep 15
for i in $(seq 1 "$RUNS"); do
  echo "[run] HPA #$i"
  run_one "hpa" "$i"
done
$KUBECTL delete -n "$APP_NS" -f "$HPA_FILE" --ignore-not-found >/dev/null 2>&1 || true

echo "[phase] ThriveScale"
$KUBECTL scale deploy/custom-autoscaler -n "$CONTROL_NS" --replicas=1
sleep 20
for i in $(seq 1 "$RUNS"); do
  echo "[run] ThriveScale #$i"
  run_one "thrivescale" "$i"
done

$PYTHON scripts/analyze_eval.py \
  --glob "$RESULT_DIR/results_demo_*.csv" \
  --scenario demo \
  --default-slo-ms "$CLIENT_SLO_MS" \
  --slo-file "$SLO_FILE" \
  --namespace "$APP_NS" \
  --slo-target svc-cpu \
  --warmup-seconds "$WARMUP_SECONDS" \
  --plot \
  --markdown-out "$ANALYSIS_DIR/demo_thrive_vs_hpa.md"

for p in results/violation_vs_cost.png results/p90_vs_time.png results/replicas_vs_time.png; do
  if [[ -f "$p" ]]; then
    mv "$p" "$ANALYSIS_DIR/"
  fi
done

echo "[done] demo comparison completed"
