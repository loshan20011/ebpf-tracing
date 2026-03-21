#!/usr/bin/env bash
set -euo pipefail

APP_NS="${APP_NS:-sock-shop}"
CONTROL_NS="${CONTROL_NS:-thrive-scale}"
SLO_FILE="${SLO_FILE:-deploy/03-evaluation/sockshop-slos.yaml}"
HPA_FILE="${HPA_FILE:-deploy/03-evaluation/hpa-sockshop.yaml}"
K6_MANIFEST="${K6_MANIFEST:-deploy/03-evaluation/workloads/k6-sockshop-mix.yaml}"
K6_JOB_NAME="${K6_JOB_NAME:-k6-sockshop-mix}"
RUNS="${RUNS:-3}"
DURATION="${DURATION:-180}"
SAMPLE_INTERVAL="${SAMPLE_INTERVAL:-2}"
WARMUP_SECONDS="${WARMUP_SECONDS:-20}"
SAMPLE_DEPLOYMENT="${SAMPLE_DEPLOYMENT:-}"
SETTLE_TIMEOUT_S="${SETTLE_TIMEOUT_S:-300}"
SETTLE_POLL_S="${SETTLE_POLL_S:-5}"
POST_SETTLE_COOLDOWN_S="${POST_SETTLE_COOLDOWN_S:-20}"
HPA_PRE_RUN_STABILIZE_S="${HPA_PRE_RUN_STABILIZE_S:-10}"
BASELINE_WARMUP_SECONDS="${BASELINE_WARMUP_SECONDS:-60}"
BASELINE_WARMUP_RPS="${BASELINE_WARMUP_RPS:-10}"
CONTROL_PLANE_STEADY_WAIT_S="${CONTROL_PLANE_STEADY_WAIT_S:-10}"
RESULT_DIR="${RESULT_DIR:-results/matrix}"
ANALYSIS_DIR="${ANALYSIS_DIR:-results/analysis}"
ENV_DIR="${ENV_DIR:-results/environment}"
PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
KUBECTL="${KUBECTL:-kubectl}"

mkdir -p "$RESULT_DIR" "$ANALYSIS_DIR" "$ENV_DIR"

if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

LB_HOST="$($KUBECTL get svc aggregator -n "$CONTROL_NS" -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
if [[ -n "$LB_HOST" ]]; then
  AGGREGATOR_URL="http://${LB_HOST}:8000"
else
  NODE_PORT="$($KUBECTL get svc aggregator -n "$CONTROL_NS" -o jsonpath='{.spec.ports[0].nodePort}')"
  NODE_IP="$($KUBECTL get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="ExternalIP")].address}' 2>/dev/null || true)"
  if [[ -z "$NODE_IP" ]]; then
    NODE_IP="$($KUBECTL get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')"
  fi
  AGGREGATOR_URL="http://${NODE_IP}:${NODE_PORT}"
fi

echo "[info] Aggregator URL: $AGGREGATOR_URL"

FRONTEND_URL="${FRONTEND_URL:-}"
if [[ -z "$FRONTEND_URL" ]]; then
  FE_LB_HOST="$($KUBECTL get svc front-end -n "$APP_NS" -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
  if [[ -n "$FE_LB_HOST" ]]; then
    FRONTEND_URL="http://${FE_LB_HOST}"
  fi
fi
if [[ -z "$FRONTEND_URL" || "$FRONTEND_URL" == "http://" ]]; then
  FE_NODE_PORT="$($KUBECTL get svc front-end -n "$APP_NS" -o jsonpath='{.spec.ports[0].nodePort}')"
  FE_NODE_IP="$($KUBECTL get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="ExternalIP")].address}' 2>/dev/null || true)"
  if [[ -z "$FE_NODE_IP" ]]; then
    FE_NODE_IP="$($KUBECTL get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')"
  fi
  FRONTEND_URL="http://${FE_NODE_IP}:${FE_NODE_PORT}"
fi
echo "[info] Frontend URL: $FRONTEND_URL"

$KUBECTL get nodes -o wide > "$ENV_DIR/environment_nodes.txt"
$KUBECTL get pods -A -o wide > "$ENV_DIR/environment_pods_all.txt"
$KUBECTL get svc -A > "$ENV_DIR/environment_services_all.txt"
$KUBECTL get serviceslos -n "$APP_NS" -o yaml > "$ENV_DIR/environment_slos_${APP_NS}.yaml"
$KUBECTL get deploy -n "$CONTROL_NS" aggregator custom-autoscaler -o yaml > "$ENV_DIR/environment_control_deploys_${CONTROL_NS}.yaml" || true
$KUBECTL get ds -n "$CONTROL_NS" bpf-agent -o yaml > "$ENV_DIR/environment_agent_${CONTROL_NS}.yaml" || true

$PYTHON_BIN scripts/validate_slos.py --file "$SLO_FILE" --namespace "$APP_NS"

reset_replicas_to_min() {
  SLO_FILE="$SLO_FILE" APP_NS="$APP_NS" KUBECTL="$KUBECTL" PYTHON_BIN="$PYTHON_BIN" "$PYTHON_BIN" - <<'PY'
import os
import subprocess
from pathlib import Path
import yaml

slo_file = Path(os.environ["SLO_FILE"])
app_ns = os.environ["APP_NS"]
kubectl = os.environ.get("KUBECTL", "kubectl")
for doc in yaml.safe_load_all(slo_file.read_text()):
    if not doc or doc.get("kind") != "ServiceSLO":
        continue
    spec = doc.get("spec", {})
    target = spec.get("targetDeployment")
    min_rep = int(spec.get("minReplicas", 1))
    if target:
        subprocess.run([kubectl, "scale", f"deploy/{target}", "-n", app_ns, f"--replicas={min_rep}"], check=False)
PY
}

load_slo_targets() {
  SLO_FILE="$SLO_FILE" PYTHON_BIN="$PYTHON_BIN" "$PYTHON_BIN" - <<'PY'
import os
import yaml
from pathlib import Path

slo_file = Path(os.environ["SLO_FILE"])
for doc in yaml.safe_load_all(slo_file.read_text()):
    if not doc or doc.get("kind") != "ServiceSLO":
        continue
    spec = doc.get("spec", {})
    target = spec.get("targetDeployment")
    if not target:
        continue
    min_rep = int(spec.get("minReplicas", 1))
    print(f"{target}:{min_rep}")
PY
}

wait_for_settle() {
  local t=0
  while [[ "$t" -lt "$SETTLE_TIMEOUT_S" ]]; do
    local pending=()
    local active_job
    active_job="$($KUBECTL get job -n "$APP_NS" "$K6_JOB_NAME" -o jsonpath='{.status.active}' 2>/dev/null || true)"

    if [[ -n "$active_job" && "$active_job" != "0" ]]; then
      pending+=("job/${K6_JOB_NAME}:active=${active_job}")
    fi

    local entry dep min_rep desired ready
    for entry in "${SLO_TARGETS[@]}"; do
      dep="${entry%%:*}"
      min_rep="${entry##*:}"
      desired="$($KUBECTL get deploy "$dep" -n "$APP_NS" -o jsonpath='{.spec.replicas}' 2>/dev/null || true)"
      ready="$($KUBECTL get deploy "$dep" -n "$APP_NS" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)"
      desired="${desired:-0}"
      ready="${ready:-0}"
      if [[ "$desired" != "$min_rep" || "$ready" != "$min_rep" ]]; then
        pending+=("${dep}:desired=${desired},ready=${ready},min=${min_rep}")
      fi
    done

    if [[ "${#pending[@]}" -eq 0 ]]; then
      echo "[ok] pre-run settle reached (all SLO targets at min replicas and ready)"
      return 0
    fi

    echo "[wait] settling (${t}s/${SETTLE_TIMEOUT_S}s): ${pending[*]}"
    sleep "$SETTLE_POLL_S"
    t=$((t + SETTLE_POLL_S))
  done

  echo "[warn] settle timeout after ${SETTLE_TIMEOUT_S}s; continuing run"
}

wait_control_plane_ready() {
  $KUBECTL rollout status -n "$CONTROL_NS" deploy/aggregator --timeout=240s
  $KUBECTL rollout status -n "$CONTROL_NS" deploy/custom-autoscaler --timeout=240s || true
}

reset_control_state() {
  local desired_controller_replicas="$1"
  echo "[phase] reset control state"
  if ! curl -fsS -m 8 "$AGGREGATOR_URL/api/reset" >/dev/null 2>&1; then
    echo "[warn] aggregator /api/reset failed; falling back to redis FLUSHALL"
    $KUBECTL exec -n "$CONTROL_NS" deploy/redis -- redis-cli FLUSHALL >/dev/null 2>&1 || true
  fi
  $KUBECTL rollout restart -n "$CONTROL_NS" deploy/aggregator
  $KUBECTL rollout restart -n "$CONTROL_NS" deploy/custom-autoscaler
  $KUBECTL scale deploy/custom-autoscaler -n "$CONTROL_NS" --replicas="$desired_controller_replicas"
  wait_control_plane_ready
  sleep "$CONTROL_PLANE_STEADY_WAIT_S"
}

run_baseline_warmup() {
  local scaler="$1"
  local run_id="$2"
  local warmup_csv="$RESULT_DIR/results_sockshop_${scaler}_run${run_id}.baseline_warmup.csv"
  echo "[phase] baseline warm-up (${BASELINE_WARMUP_SECONDS}s @ ${BASELINE_WARMUP_RPS} rps)"
  curl -fsS -m 8 -X POST "$AGGREGATOR_URL/api/control/runq-baseline/start" >/dev/null 2>&1 || true
  "$PYTHON_BIN" src/load-generator/eval_harness.py \
    --url "$FRONTEND_URL" \
    --deployment "$SAMPLE_DEPLOYMENT" \
    --namespace "$APP_NS" \
    --profile generic \
    --mode steady \
    --duration "$BASELINE_WARMUP_SECONDS" \
    --base-rps "$BASELINE_WARMUP_RPS" \
    --burst-rps "$BASELINE_WARMUP_RPS" \
    --warmup-seconds 0 \
    --aggregator-url "$AGGREGATOR_URL" \
    --control-target "$SAMPLE_DEPLOYMENT" \
    --csv "$warmup_csv"
  curl -fsS -m 8 -X POST "$AGGREGATOR_URL/api/control/runq-baseline/stop" >/dev/null 2>&1 || true
}

mapfile -t SLO_TARGETS < <(load_slo_targets)
if [[ -z "$SAMPLE_DEPLOYMENT" && "${#SLO_TARGETS[@]}" -gt 0 ]]; then
  SAMPLE_DEPLOYMENT="${SLO_TARGETS[0]%%:*}"
fi
if [[ -z "$SAMPLE_DEPLOYMENT" ]]; then
  echo "[error] SAMPLE_DEPLOYMENT is empty and no ServiceSLO targets were discovered"
  exit 1
fi

run_one() {
  local scaler="$1"
  local run_id="$2"
  local use_hpa="${3:-0}"
  local csv="$RESULT_DIR/results_sockshop_${scaler}_run${run_id}.csv"
  local k6log="$ANALYSIS_DIR/k6_${scaler}_run${run_id}.log"
  local fail_json="$ANALYSIS_DIR/failure_categories_${scaler}_run${run_id}.json"
  local controller_replicas="1"
  if [[ "$use_hpa" == "1" ]]; then
    controller_replicas="0"
  fi

  echo "[run] ${scaler} #${run_id}"
  reset_control_state "$controller_replicas"
  reset_replicas_to_min
  $KUBECTL delete -n "$APP_NS" job "$K6_JOB_NAME" --ignore-not-found >/dev/null 2>&1 || true
  wait_for_settle
  run_baseline_warmup "$scaler" "$run_id"
  curl -fsS -m 8 -X POST "$AGGREGATOR_URL/api/control/runq-baseline/stop" >/dev/null 2>&1 || true
  wait_for_settle
  sleep "$POST_SETTLE_COOLDOWN_S"

  if [[ "$use_hpa" == "1" ]]; then
    $KUBECTL apply -n "$APP_NS" -f "$HPA_FILE"
    sleep "$HPA_PRE_RUN_STABILIZE_S"
  fi

  "$PYTHON_BIN" scripts/sample_cluster_metrics.py \
    --aggregator-url "$AGGREGATOR_URL" \
    --namespace "$APP_NS" \
    --deployment "$SAMPLE_DEPLOYMENT" \
    --duration "$((DURATION + 8))" \
    --sample-interval "$SAMPLE_INTERVAL" \
    --warmup-seconds "$WARMUP_SECONDS" \
    --csv "$csv" &
  local sampler_pid=$!

  $KUBECTL apply -n "$APP_NS" -f "$K6_MANIFEST"

  if ! $KUBECTL wait -n "$APP_NS" --for=condition=complete "job/${K6_JOB_NAME}" --timeout=420s; then
    echo "[error] K6 job did not complete cleanly for ${scaler} run ${run_id}"
    $KUBECTL logs -n "$APP_NS" "job/${K6_JOB_NAME}" --tail=200 || true
    kill "$sampler_pid" >/dev/null 2>&1 || true
    wait "$sampler_pid" >/dev/null 2>&1 || true
    return 1
  fi

  $KUBECTL logs -n "$APP_NS" "job/${K6_JOB_NAME}" > "$k6log" || true
  K6_LOG_PATH="$k6log" OUT_PATH="$fail_json" "$PYTHON_BIN" - <<'PY'
import json
import os
import re

path = os.environ["K6_LOG_PATH"]
out = os.environ["OUT_PATH"]
text = ""
try:
    text = open(path, "r", encoding="utf-8", errors="ignore").read()
except Exception:
    pass

timeout_count = len(re.findall(r"(request timeout|i/o timeout)", text, flags=re.IGNORECASE))
conn_refused_count = len(re.findall(r"connection refused", text, flags=re.IGNORECASE))
status_codes = [int(x) for x in re.findall(r"status (?:was|is) (\d{3})", text, flags=re.IGNORECASE)]
http_5xx_count = sum(1 for c in status_codes if 500 <= c < 600)
dropped_match = re.search(r"dropped_iterations[.\s:]+([0-9,]+)", text)
dropped_iterations = int(dropped_match.group(1).replace(",", "")) if dropped_match else 0
queue_sat_markers = len(re.findall(r"Insufficient VUs", text, flags=re.IGNORECASE))

payload = {
    "timeout": timeout_count,
    "connection_refused": conn_refused_count,
    "5xx": http_5xx_count,
    "client_side_dropped_request": dropped_iterations,
    "queue_saturation": queue_sat_markers,
}
open(out, "w", encoding="utf-8").write(json.dumps(payload, indent=2, sort_keys=True))
print(json.dumps(payload, sort_keys=True))
PY
  wait "$sampler_pid"
  $KUBECTL delete -n "$APP_NS" job "$K6_JOB_NAME" --ignore-not-found >/dev/null 2>&1 || true

  if [[ "$use_hpa" == "1" ]]; then
    $KUBECTL delete -n "$APP_NS" -f "$HPA_FILE" --ignore-not-found || true
  fi
}

echo "[phase] HPA baseline"
$KUBECTL delete -n "$APP_NS" -f "$HPA_FILE" --ignore-not-found >/dev/null 2>&1 || true
for i in $(seq 1 "$RUNS"); do
  run_one "hpa" "$i" "1"
  sleep 15
done

echo "[phase] ThriveScale"
for i in $(seq 1 "$RUNS"); do
  run_one "thrivescale" "$i" "0"
  sleep 15
done

"$PYTHON_BIN" scripts/analyze_eval.py \
  --glob "$RESULT_DIR/results_sockshop_*.csv" \
  --scenario sockshop \
  --slo-file "$SLO_FILE" \
  --slo-target "$SAMPLE_DEPLOYMENT" \
  --warmup-seconds "$WARMUP_SECONDS" \
  --plot \
  --markdown-out "$ANALYSIS_DIR/thesis_sockshop_summary.md"

for p in results/violation_vs_cost.png results/p90_vs_time.png results/replicas_vs_time.png; do
  if [[ -f "$p" ]]; then
    mv "$p" "$ANALYSIS_DIR/"
  fi
done

echo "[done] K6 matrix execution completed"
