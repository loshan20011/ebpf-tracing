#!/usr/bin/env bash
set -euo pipefail

APP_NS="${APP_NS:-sock-shop}"
CONTROL_NS="${CONTROL_NS:-thrive-scale}"
SLO_FILE="${SLO_FILE:-deploy/03-evaluation/sockshop-slos.yaml}"
CALIBRATED_SLO_FILE="${CALIBRATED_SLO_FILE:-deploy/03-evaluation/sockshop-slos.calibrated.yaml}"
CALIBRATE_SLOS="${CALIBRATE_SLOS:-1}"
HPA_FILE="${HPA_FILE:-deploy/03-evaluation/hpa-sockshop.yaml}"
RUNS="${RUNS:-3}"
DURATION="${DURATION:-180}"
SAMPLE_INTERVAL="${SAMPLE_INTERVAL:-2}"
TIMEOUT="${TIMEOUT:-8}"
RESULT_DIR="${RESULT_DIR:-results/matrix}"
ANALYSIS_DIR="${ANALYSIS_DIR:-results/analysis}"
ENV_DIR="${ENV_DIR:-results/environment}"
FRONTEND_URL="${FRONTEND_URL:-}"
WORKLOAD_PROFILE="${WORKLOAD_PROFILE:-sockshop}"
MIX_FILE="${MIX_FILE:-deploy/03-evaluation/workloads/sockshop-ew-mix.yaml}"
WARMUP_SECONDS="${WARMUP_SECONDS:-20}"
SETTLE_TIMEOUT_SECONDS="${SETTLE_TIMEOUT_SECONDS:-240}"
SETTLE_POLL_SECONDS="${SETTLE_POLL_SECONDS:-5}"
BASELINE_WARMUP_SECONDS="${BASELINE_WARMUP_SECONDS:-60}"
BASELINE_WARMUP_RPS="${BASELINE_WARMUP_RPS:-10}"
CONTROL_PLANE_STEADY_WAIT_S="${CONTROL_PLANE_STEADY_WAIT_S:-10}"
KUBECTL="${KUBECTL:-kubectl}"
MODE="${MODE:-thrivescale}"

mkdir -p "$RESULT_DIR" "$ANALYSIS_DIR" "$ENV_DIR"

if [[ -z "$FRONTEND_URL" ]]; then
  LB_HOST="$($KUBECTL get svc front-end -n "$APP_NS" -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
  if [[ -n "$LB_HOST" ]]; then
    FRONTEND_URL="http://${LB_HOST}"
  fi
fi
if [[ -z "$FRONTEND_URL" || "$FRONTEND_URL" == "http://" ]]; then
  NODE_PORT="$($KUBECTL get svc front-end -n "$APP_NS" -o jsonpath='{.spec.ports[0].nodePort}')"
  NODE_IP="$($KUBECTL get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="ExternalIP")].address}')"
  if [[ -z "$NODE_IP" ]]; then
    NODE_IP="$($KUBECTL get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')"
  fi
  FRONTEND_URL="http://${NODE_IP}:${NODE_PORT}"
fi

LB_HOST="$($KUBECTL get svc aggregator -n "$CONTROL_NS" -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
if [[ -n "$LB_HOST" ]]; then
  AGGREGATOR_URL="http://${LB_HOST}:8000"
else
  AGG_NODE_PORT="$($KUBECTL get svc aggregator -n "$CONTROL_NS" -o jsonpath='{.spec.ports[0].nodePort}')"
  AGG_NODE_IP="$($KUBECTL get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="ExternalIP")].address}' 2>/dev/null || true)"
  if [[ -z "$AGG_NODE_IP" ]]; then
    AGG_NODE_IP="$($KUBECTL get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')"
  fi
  AGGREGATOR_URL="http://${AGG_NODE_IP}:${AGG_NODE_PORT}"
fi

if $KUBECTL get deploy -n "$APP_NS" svc-cpu >/dev/null 2>&1 || $KUBECTL get deploy -n "$APP_NS" svc-io >/dev/null 2>&1; then
  echo "[error] synthetic demo workloads found in $APP_NS (svc-cpu/svc-io). Remove them before benchmark runs."
  exit 1
fi

if $KUBECTL get hpa -n "$APP_NS" >/dev/null 2>&1; then
  echo "[info] deleting pre-existing HPAs in $APP_NS for clean isolation"
  $KUBECTL delete hpa --all -n "$APP_NS" || true
fi

not_running="$($KUBECTL get pods -n "$APP_NS" --no-headers 2>/dev/null | awk '$3!="Running" && $3!="Completed" {print $1}')"
if [[ -n "$not_running" ]]; then
  echo "[error] app namespace has non-running pods:"
  echo "$not_running"
  exit 1
fi

if ! curl -sS -m 5 -I "$FRONTEND_URL" >/dev/null; then
  echo "[error] frontend URL unreachable: $FRONTEND_URL"
  exit 1
fi

effective_slo_file="$SLO_FILE"
if [[ "$CALIBRATE_SLOS" == "1" ]]; then
  echo "[phase] SLO calibration baseline"
  python3 scripts/calibrate_slos.py \
    --app-namespace "$APP_NS" \
    --control-namespace "$CONTROL_NS" \
    --slo-file "$SLO_FILE" \
    --output "$CALIBRATED_SLO_FILE" \
    --frontend-url "$FRONTEND_URL" \
    --mix-file "$MIX_FILE" \
    --duration "$DURATION" \
    --warmup-seconds "$WARMUP_SECONDS"
  effective_slo_file="$CALIBRATED_SLO_FILE"
  $KUBECTL apply -n "$APP_NS" -f "$effective_slo_file"
fi

python3 scripts/validate_slos.py --file "$effective_slo_file" --namespace "$APP_NS"

$KUBECTL get nodes -o wide > "$ENV_DIR/environment_nodes.txt"
$KUBECTL get pods -A -o wide > "$ENV_DIR/environment_pods_all.txt"
$KUBECTL get svc -A > "$ENV_DIR/environment_services_all.txt"
$KUBECTL get serviceslos -n "$APP_NS" -o yaml > "$ENV_DIR/environment_slos_${APP_NS}.yaml"
$KUBECTL get deploy -n "$CONTROL_NS" aggregator custom-autoscaler -o yaml > "$ENV_DIR/environment_control_deploys_${CONTROL_NS}.yaml" || true
$KUBECTL get ds -n "$CONTROL_NS" bpf-agent -o yaml > "$ENV_DIR/environment_agent_${CONTROL_NS}.yaml" || true

echo "[info] Frontend URL: $FRONTEND_URL"
echo "[info] Aggregator URL: $AGGREGATOR_URL"
echo "[info] SLO file: $effective_slo_file"

autotune_replicas_to_min() {
  SLO_FILE="$effective_slo_file" APP_NS="$APP_NS" python3 - <<'PY'
import os
import subprocess
from pathlib import Path
import yaml

slo_file = Path(os.environ["SLO_FILE"])
app_ns = os.environ["APP_NS"]
for doc in yaml.safe_load_all(slo_file.read_text()):
    if not doc or doc.get("kind") != "ServiceSLO":
        continue
    spec = doc.get("spec", {})
    target = spec.get("targetDeployment")
    min_rep = int(spec.get("minReplicas", 1))
    if target:
        subprocess.run([os.environ.get("KUBECTL", "kubectl"), "scale", f"deploy/{target}", "-n", app_ns, f"--replicas={min_rep}"], check=False)
PY
}

wait_until_slo_mins_ready() {
  local deadline=$((SECONDS + SETTLE_TIMEOUT_SECONDS))
  while (( SECONDS < deadline )); do
    if SLO_FILE="$effective_slo_file" APP_NS="$APP_NS" python3 - <<'PY'
import os
import subprocess
from pathlib import Path
import yaml

slo_file = Path(os.environ["SLO_FILE"])
app_ns = os.environ["APP_NS"]
ok = True

for doc in yaml.safe_load_all(slo_file.read_text()):
    if not doc or doc.get("kind") != "ServiceSLO":
        continue
    spec = doc.get("spec", {})
    target = spec.get("targetDeployment")
    if not target:
        continue
    min_rep = int(spec.get("minReplicas", 1))
    cp = subprocess.run(
        [os.environ.get("KUBECTL", "kubectl"), "get", "deploy", target, "-n", app_ns, "-o", "jsonpath={.spec.replicas}:{.status.readyReplicas}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if cp.returncode != 0:
        ok = False
        continue
    raw = (cp.stdout or "").strip()
    spec_rep_s, _, ready_rep_s = raw.partition(":")
    try:
        spec_rep = int(spec_rep_s or "0")
        ready_rep = int(ready_rep_s or "0")
    except Exception:
        ok = False
        continue
    if spec_rep > min_rep or ready_rep != spec_rep:
        ok = False

raise SystemExit(0 if ok else 1)
PY
    then
      echo "[ok] settle complete: deployments at SLO min replicas"
      return 0
    fi
    sleep "$SETTLE_POLL_SECONDS"
  done
  echo "[warn] settle timeout (${SETTLE_TIMEOUT_SECONDS}s); proceeding anyway"
  return 0
}

wait_control_plane_ready() {
  $KUBECTL rollout status -n "$CONTROL_NS" deploy/aggregator --timeout=240s
  $KUBECTL rollout status -n "$CONTROL_NS" deploy/custom-autoscaler --timeout=240s || true
}

reset_control_state() {
  echo "[phase] reset control state"
  if ! curl -fsS -m 8 "$AGGREGATOR_URL/api/reset" >/dev/null 2>&1; then
    echo "[warn] aggregator /api/reset failed; falling back to redis FLUSHALL"
    $KUBECTL exec -n "$CONTROL_NS" deploy/redis -- redis-cli FLUSHALL >/dev/null 2>&1 || true
  fi
  $KUBECTL rollout restart -n "$CONTROL_NS" deploy/aggregator
  $KUBECTL rollout restart -n "$CONTROL_NS" deploy/custom-autoscaler
  wait_control_plane_ready
  sleep "$CONTROL_PLANE_STEADY_WAIT_S"
}

run_baseline_warmup() {
  local scaler="$1"
  local run_id="$2"
  local warmup_csv="$RESULT_DIR/results_sockshop_${scaler}_run${run_id}.baseline_warmup.csv"
  echo "[phase] baseline warm-up (${BASELINE_WARMUP_SECONDS}s @ ${BASELINE_WARMUP_RPS} rps)"
  curl -fsS -m 8 -X POST "$AGGREGATOR_URL/api/control/runq-baseline/start" >/dev/null 2>&1 || true
  python3 src/load-generator/eval_harness.py \
    --url "$FRONTEND_URL" \
    --deployment front-end \
    --namespace "$APP_NS" \
    --profile generic \
    --mode steady \
    --duration "$BASELINE_WARMUP_SECONDS" \
    --base-rps "$BASELINE_WARMUP_RPS" \
    --burst-rps "$BASELINE_WARMUP_RPS" \
    --warmup-seconds 0 \
    --aggregator-url "$AGGREGATOR_URL" \
    --control-target front-end \
    --csv "$warmup_csv"
  curl -fsS -m 8 -X POST "$AGGREGATOR_URL/api/control/runq-baseline/stop" >/dev/null 2>&1 || true
}

run_one() {
  local scaler="$1"
  local run_id="$2"
  local csv="$RESULT_DIR/results_sockshop_${scaler}_run${run_id}.csv"

  reset_control_state
  autotune_replicas_to_min
  wait_until_slo_mins_ready
  python3 scripts/benchmark_preflight.py \
    --app-namespace "$APP_NS" \
    --control-namespace "$CONTROL_NS" \
    --aggregator-url "$AGGREGATOR_URL" \
    --services front-end catalogue carts orders \
    --expected-replicas 1 \
    --mode "$scaler"
  run_baseline_warmup "$scaler" "$run_id"
  curl -fsS -m 8 -X POST "$AGGREGATOR_URL/api/control/runq-baseline/stop" >/dev/null 2>&1 || true

  python3 src/load-generator/eval_harness.py \
    --url "$FRONTEND_URL" \
    --deployment front-end \
    --namespace "$APP_NS" \
    --profile "$WORKLOAD_PROFILE" \
    --mix-file "$MIX_FILE" \
    --duration "$DURATION" \
    --sample-interval "$SAMPLE_INTERVAL" \
    --timeout "$TIMEOUT" \
    --warmup-seconds "$WARMUP_SECONDS" \
    --aggregator-url "$AGGREGATOR_URL" \
    --control-target front-end \
    --csv "$csv"
}

# HPA baseline phase
echo "[phase] HPA baseline"
$KUBECTL scale deploy/custom-autoscaler -n "$CONTROL_NS" --replicas=0
$KUBECTL apply -n "$APP_NS" -f "$HPA_FILE"
sleep 20
for i in $(seq 1 "$RUNS"); do
  echo "[run] HPA #$i"
  run_one "hpa" "$i"
  wait_until_slo_mins_ready
done
if ! $KUBECTL delete -n "$APP_NS" -f "$HPA_FILE" --ignore-not-found; then
  echo "[warn] failed to delete HPA manifest cleanly (transient API error); continuing to ThriveScale phase"
fi

# ThriveScale phase
echo "[phase] ThriveScale"
$KUBECTL scale deploy/custom-autoscaler -n "$CONTROL_NS" --replicas=1
sleep 25
for i in $(seq 1 "$RUNS"); do
  echo "[run] ThriveScale #$i"
  run_one "thrivescale" "$i"
  wait_until_slo_mins_ready
done

python3 scripts/analyze_eval.py \
  --glob "$RESULT_DIR/results_sockshop_*.csv" \
  --scenario sockshop \
  --slo-file "$effective_slo_file" \
  --slo-target front-end \
  --warmup-seconds "$WARMUP_SECONDS" \
  --plot \
  --markdown-out "$ANALYSIS_DIR/thesis_sockshop_summary.md"

for p in results/violation_vs_cost.png results/p90_vs_time.png results/replicas_vs_time.png; do
  if [[ -f "$p" ]]; then
    mv "$p" "$ANALYSIS_DIR/"
  fi
done

echo "[done] matrix execution completed"
