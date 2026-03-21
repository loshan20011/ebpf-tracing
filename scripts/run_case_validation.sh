#!/usr/bin/env bash
set -euo pipefail

APP_NS="${APP_NS:-sock-shop}"
CONTROL_NS="${CONTROL_NS:-thrive-scale}"
SLO_FILE="${SLO_FILE:-deploy/03-evaluation/sockshop-slos.yaml}"
KUBECTL="${KUBECTL:-kubectl}"

OUT_DIR="${OUT_DIR:-/tmp/case-artifacts}"
TS="${TS:-$(date +%Y%m%d_%H%M%S)}"

FRONTEND_URL="${FRONTEND_URL:-}"
AGGREGATOR_URL="${AGGREGATOR_URL:-}"
CASE4_MIX="${CASE4_MIX:-deploy/03-evaluation/workloads/sockshop-dual-hot-backend.yaml}"

BASELINE_WARMUP_SECONDS="${BASELINE_WARMUP_SECONDS:-30}"
BASELINE_WARMUP_RPS="${BASELINE_WARMUP_RPS:-5}"
CASE3_BASELINE_WARMUP_SECONDS="${CASE3_BASELINE_WARMUP_SECONDS:-0}"
CONTROL_PLANE_STEADY_WAIT_S="${CONTROL_PLANE_STEADY_WAIT_S:-8}"
SETTLE_TIMEOUT_SECONDS="${SETTLE_TIMEOUT_SECONDS:-240}"
SETTLE_POLL_SECONDS="${SETTLE_POLL_SECONDS:-5}"
TELEMETRY_CHECK_RETRIES="${TELEMETRY_CHECK_RETRIES:-6}"
TELEMETRY_CHECK_INTERVAL_S="${TELEMETRY_CHECK_INTERVAL_S:-2}"
TELEMETRY_MIN_NET_SAMPLES="${TELEMETRY_MIN_NET_SAMPLES:-1}"
TELEMETRY_MIN_RUNQ_SAMPLES="${TELEMETRY_MIN_RUNQ_SAMPLES:-1}"
TELEMETRY_MAX_AGENT_DROPPED="${TELEMETRY_MAX_AGENT_DROPPED:-0}"
RESET_DEPLOYMENTS="${RESET_DEPLOYMENTS:-}"
INCLUDE_DB_DEPLOYS="${INCLUDE_DB_DEPLOYS:-false}"

mkdir -p "$OUT_DIR"

resolve_frontend_url() {
  if [[ -n "$FRONTEND_URL" ]]; then
    echo "$FRONTEND_URL"
    return
  fi
  local lb_host node_port node_ip
  lb_host="$($KUBECTL get svc front-end -n "$APP_NS" -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
  if [[ -n "$lb_host" ]]; then
    echo "http://${lb_host}"
    return
  fi
  node_port="$($KUBECTL get svc front-end -n "$APP_NS" -o jsonpath='{.spec.ports[0].nodePort}')"
  node_ip="$($KUBECTL get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="ExternalIP")].address}' 2>/dev/null || true)"
  if [[ -z "$node_ip" ]]; then
    node_ip="$($KUBECTL get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')"
  fi
  echo "http://${node_ip}:${node_port}"
}

resolve_aggregator_url() {
  if [[ -n "$AGGREGATOR_URL" ]]; then
    echo "$AGGREGATOR_URL"
    return
  fi
  local lb_host node_port node_ip
  lb_host="$($KUBECTL get svc aggregator -n "$CONTROL_NS" -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
  if [[ -n "$lb_host" ]]; then
    echo "http://${lb_host}:8000"
    return
  fi
  node_port="$($KUBECTL get svc aggregator -n "$CONTROL_NS" -o jsonpath='{.spec.ports[0].nodePort}')"
  node_ip="$($KUBECTL get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="ExternalIP")].address}' 2>/dev/null || true)"
  if [[ -z "$node_ip" ]]; then
    node_ip="$($KUBECTL get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')"
  fi
  echo "http://${node_ip}:${node_port}"
}

FRONTEND_URL="$(resolve_frontend_url)"
AGGREGATOR_URL="$(resolve_aggregator_url)"

wait_control_plane_ready() {
  $KUBECTL rollout status -n "$CONTROL_NS" deploy/aggregator --timeout=240s
  $KUBECTL rollout status -n "$CONTROL_NS" deploy/custom-autoscaler --timeout=240s
}

pause_autoscaler_for_settle() {
  $KUBECTL scale deploy/custom-autoscaler -n "$CONTROL_NS" --replicas=0 >/dev/null 2>&1 || true
}

enable_autoscaler() {
  $KUBECTL scale deploy/custom-autoscaler -n "$CONTROL_NS" --replicas=1 >/dev/null 2>&1 || true
  $KUBECTL rollout status -n "$CONTROL_NS" deploy/custom-autoscaler --timeout=240s
}

reset_replicas_to_min() {
  RESET_DEPLOYMENTS="$RESET_DEPLOYMENTS" INCLUDE_DB_DEPLOYS="$INCLUDE_DB_DEPLOYS" APP_NS="$APP_NS" KUBECTL="$KUBECTL" python3 - <<'PY'
import os
import json
import subprocess

app_ns = os.environ["APP_NS"]
kubectl = os.environ.get("KUBECTL", "kubectl")
explicit = [x.strip() for x in os.environ.get("RESET_DEPLOYMENTS", "").split(",") if x.strip()]
include_db = os.environ.get("INCLUDE_DB_DEPLOYS", "false").lower() == "true"

targets = []
if explicit:
    targets = explicit
else:
    cp = subprocess.run(
        [kubectl, "get", "deploy", "-n", app_ns, "-o", "json"],
        check=False,
        capture_output=True,
        text=True,
    )
    payload = {}
    if cp.returncode == 0:
        try:
            payload = json.loads(cp.stdout or "{}")
        except Exception:
            payload = {}
    for item in payload.get("items", []):
        name = str(item.get("metadata", {}).get("name", "")).strip()
        if not name:
            continue
        if (not include_db) and name.endswith("-db"):
            continue
        targets.append(name)

for target in sorted(set(targets)):
    subprocess.run([kubectl, "scale", f"deploy/{target}", "-n", app_ns, "--replicas=1"], check=False)
PY
}

wait_until_slo_mins_ready() {
  local deadline=$((SECONDS + SETTLE_TIMEOUT_SECONDS))
  while ((SECONDS < deadline)); do
    if RESET_DEPLOYMENTS="$RESET_DEPLOYMENTS" INCLUDE_DB_DEPLOYS="$INCLUDE_DB_DEPLOYS" APP_NS="$APP_NS" KUBECTL="$KUBECTL" python3 - <<'PY'
import os
import json
import subprocess

app_ns = os.environ["APP_NS"]
kubectl = os.environ.get("KUBECTL", "kubectl")
explicit = [x.strip() for x in os.environ.get("RESET_DEPLOYMENTS", "").split(",") if x.strip()]
include_db = os.environ.get("INCLUDE_DB_DEPLOYS", "false").lower() == "true"
ok = True
min_rep = 1

targets = []
if explicit:
    targets = explicit
else:
    cp = subprocess.run(
        [kubectl, "get", "deploy", "-n", app_ns, "-o", "json"],
        check=False,
        capture_output=True,
        text=True,
    )
    payload = {}
    if cp.returncode == 0:
        try:
            payload = json.loads(cp.stdout or "{}")
        except Exception:
            payload = {}
    for item in payload.get("items", []):
        name = str(item.get("metadata", {}).get("name", "")).strip()
        if not name:
            continue
        if (not include_db) and name.endswith("-db"):
            continue
        targets.append(name)

for target in sorted(set(targets)):
    cp = subprocess.run(
        [kubectl, "get", "deploy", target, "-n", app_ns, "-o", "jsonpath={.spec.replicas}:{.status.readyReplicas}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if cp.returncode != 0:
        ok = False
        continue
    spec_s, _, ready_s = (cp.stdout or "").strip().partition(":")
    try:
        spec_rep = int(spec_s or "0")
        ready_rep = int(ready_s or "0")
    except Exception:
        ok = False
        continue
    if spec_rep > min_rep:
        subprocess.run([kubectl, "scale", f"deploy/{target}", "-n", app_ns, f"--replicas={min_rep}"], check=False)
        ok = False
        continue
    if ready_rep != spec_rep:
        ok = False

raise SystemExit(0 if ok else 1)
PY
    then
      echo "[ok] precondition met: all target deployments are at 1/1"
      return 0
    fi
    sleep "$SETTLE_POLL_SECONDS"
  done
  echo "[error] precondition failed: not all target deployments reached 1/1 before case start"
  return 1
}

reset_control_state() {
  echo "[phase] reset control state"
  if ! curl -fsS -m 8 -X POST "$AGGREGATOR_URL/api/reset" >/dev/null 2>&1; then
    echo "[warn] aggregator /api/reset failed; falling back to redis FLUSHALL"
    $KUBECTL exec -n "$CONTROL_NS" deploy/redis -- redis-cli FLUSHALL >/dev/null 2>&1 || true
  fi
  $KUBECTL rollout restart -n "$CONTROL_NS" deploy/aggregator
  $KUBECTL rollout restart -n "$CONTROL_NS" daemonset/bpf-agent
  $KUBECTL rollout status -n "$CONTROL_NS" deploy/aggregator --timeout=240s
  $KUBECTL rollout status -n "$CONTROL_NS" daemonset/bpf-agent --timeout=240s
  sleep "$CONTROL_PLANE_STEADY_WAIT_S"
}

run_baseline_warmup() {
  local case_name="$1"
  local warmup_seconds="$BASELINE_WARMUP_SECONDS"
  if [[ "$case_name" == "case3_no_panic" ]]; then
    warmup_seconds="$CASE3_BASELINE_WARMUP_SECONDS"
  fi
  if (( warmup_seconds <= 0 )); then
    echo "[phase] baseline warm-up skipped for ${case_name}"
    return 0
  fi
  local warmup_csv="$OUT_DIR/baseline_${TS}.csv"
  echo "[phase] baseline warm-up (${warmup_seconds}s @ ${BASELINE_WARMUP_RPS} rps)"
  curl -fsS -m 8 -X POST "$AGGREGATOR_URL/api/control/runq-baseline/start" >/dev/null 2>&1 || true
  python3 src/load-generator/eval_harness.py \
    --url "$FRONTEND_URL" \
    --deployment front-end \
    --namespace "$APP_NS" \
    --profile generic \
    --mode steady \
    --duration "$warmup_seconds" \
    --base-rps "$BASELINE_WARMUP_RPS" \
    --burst-rps "$BASELINE_WARMUP_RPS" \
    --warmup-seconds 0 \
    --sample-interval 2 \
    --aggregator-url "$AGGREGATOR_URL" \
    --control-target front-end \
    --csv "$warmup_csv" >/dev/null
  curl -fsS -m 8 -X POST "$AGGREGATOR_URL/api/control/runq-baseline/stop" >/dev/null 2>&1 || true
}

check_telemetry_health_once() {
  AGGREGATOR_URL="$AGGREGATOR_URL" CONTROL_NS="$CONTROL_NS" KUBECTL="$KUBECTL" \
  TELEMETRY_MIN_NET_SAMPLES="$TELEMETRY_MIN_NET_SAMPLES" \
  TELEMETRY_MIN_RUNQ_SAMPLES="$TELEMETRY_MIN_RUNQ_SAMPLES" \
  TELEMETRY_MAX_AGENT_DROPPED="$TELEMETRY_MAX_AGENT_DROPPED" \
  python3 - <<'PY'
import json
import os
import re
import subprocess
import sys
import urllib.request

agg_url = os.environ["AGGREGATOR_URL"].rstrip("/")
control_ns = os.environ["CONTROL_NS"]
kubectl = os.environ.get("KUBECTL", "kubectl")
min_net = int(os.environ.get("TELEMETRY_MIN_NET_SAMPLES", "1"))
min_runq = int(os.environ.get("TELEMETRY_MIN_RUNQ_SAMPLES", "1"))
max_dropped_allowed = int(os.environ.get("TELEMETRY_MAX_AGENT_DROPPED", "0"))

try:
    with urllib.request.urlopen(f"{agg_url}/api/graph", timeout=6) as resp:
        payload = json.load(resp)
except Exception as exc:
    print(f"telemetry_status=FAIL reason=graph_fetch_error detail={exc}")
    raise SystemExit(1)

metrics = payload.get("metrics", {})
fe = metrics.get("front-end", {})
net_samples = int(fe.get("net_sample_count", 0) or 0)
runq_samples = int(fe.get("runq_sample_count", 0) or 0)
max_net_samples = net_samples
max_runq_samples = runq_samples
sum_net_samples = 0
sum_runq_samples = 0
for row in metrics.values():
    if not isinstance(row, dict):
        continue
    try:
        n = int(row.get("net_sample_count", 0) or 0)
    except Exception:
        n = 0
    try:
        r = int(row.get("runq_sample_count", 0) or 0)
    except Exception:
        r = 0
    max_net_samples = max(max_net_samples, n)
    max_runq_samples = max(max_runq_samples, r)
    sum_net_samples += n
    sum_runq_samples += r

cp = subprocess.run(
    [
        kubectl, "logs", "-n", control_ns, "deploy/aggregator", "--tail=300"
    ],
    check=False,
    capture_output=True,
    text=True,
)
max_dropped = 0
if cp.returncode == 0:
    for m in re.finditer(r"agent_dropped=(\d+)", cp.stdout or ""):
        try:
            max_dropped = max(max_dropped, int(m.group(1)))
        except Exception:
            pass

ok = True
reasons = []
effective_net_samples = max(max_net_samples, sum_net_samples)
effective_runq_samples = max(max_runq_samples, sum_runq_samples)

if effective_net_samples < min_net:
    ok = False
    reasons.append(f"net_sample_count<{min_net} (got {effective_net_samples})")
if effective_runq_samples < min_runq:
    ok = False
    reasons.append(f"runq_sample_count<{min_runq} (got {effective_runq_samples})")
if max_dropped > max_dropped_allowed:
    ok = False
    reasons.append(
        f"agent_dropped>{max_dropped_allowed} (got {max_dropped})"
    )

if ok:
    print(
        f"telemetry_status=OK net_sample_count={effective_net_samples} "
        f"runq_sample_count={effective_runq_samples} "
        f"front_end_net_sample_count={net_samples} "
        f"front_end_runq_sample_count={runq_samples} "
        f"max_agent_dropped={max_dropped}"
    )
    raise SystemExit(0)

print(
    "telemetry_status=FAIL "
    f"net_sample_count={effective_net_samples} runq_sample_count={effective_runq_samples} "
    f"front_end_net_sample_count={net_samples} front_end_runq_sample_count={runq_samples} "
    f"max_agent_dropped={max_dropped} reasons={'|'.join(reasons)}"
)
raise SystemExit(1)
PY
}

ensure_telemetry_healthy() {
  local case_name="$1"
  local i
  for ((i=1; i<=TELEMETRY_CHECK_RETRIES; i++)); do
    local line
    line="$(check_telemetry_health_once || true)"
    if [[ "$line" == telemetry_status=OK* ]]; then
      echo "[ok] telemetry healthy before ${case_name}: ${line}"
      return 0
    fi
    echo "[warn] telemetry check ${i}/${TELEMETRY_CHECK_RETRIES} before ${case_name}: ${line:-telemetry_status=FAIL unknown}"
    sleep "$TELEMETRY_CHECK_INTERVAL_S"
  done
  echo "[error] telemetry_quality_failed before ${case_name}"
  return 1
}

run_case() {
  local case_name="$1"
  shift
  echo "[case] ${case_name}"

  reset_replicas_to_min
  pause_autoscaler_for_settle
  wait_until_slo_mins_ready
  reset_control_state
  run_baseline_warmup "$case_name"
  ensure_telemetry_healthy "$case_name"
  enable_autoscaler

  local start_iso pod agg_pod run_log ctrl_log agg_log csv_path
  start_iso="$(date -u +%FT%TZ)"
  pod="$($KUBECTL get pods -n "$CONTROL_NS" -l app=autoscaler --sort-by=.metadata.creationTimestamp -o name | tail -n1)"
  agg_pod="$($KUBECTL get pods -n "$CONTROL_NS" -l app=aggregator --sort-by=.metadata.creationTimestamp -o name | tail -n1)"
  run_log="$OUT_DIR/${case_name}_${TS}.run.log"
  ctrl_log="$OUT_DIR/${case_name}_${TS}.ctrl.log"
  agg_log="$OUT_DIR/${case_name}_${TS}.agg.log"
  csv_path="$OUT_DIR/${case_name}_${TS}.csv"

  python3 src/load-generator/eval_harness.py \
    --url "$FRONTEND_URL" \
    --deployment front-end \
    --namespace "$APP_NS" \
    --aggregator-url "$AGGREGATOR_URL" \
    --control-target front-end \
    --csv "$csv_path" \
    "$@" >"$run_log" 2>&1

  $KUBECTL logs -n "$CONTROL_NS" "$pod" --since-time="$start_iso" >"$ctrl_log" 2>&1 || true
  $KUBECTL logs -n "$CONTROL_NS" "$agg_pod" --since-time="$start_iso" >"$agg_log" 2>&1 || true
}

count_or_zero() {
  local pattern="$1"
  local file="$2"
  local c
  c=$(egrep -c "$pattern" "$file" 2>/dev/null || true)
  if [[ -z "$c" ]]; then
    c=0
  fi
  echo "$c"
}

summarize_trace_nodes() {
  local file="$1"
  python3 - "$file" <<'PY'
import json
import re
import sys

ctrl = sys.argv[1]
seen = set()
with open(ctrl, "r", encoding="utf-8", errors="ignore") as f:
    for line in f:
        if "TRACE " not in line:
            continue
        payload = line.split("TRACE ", 1)[1].strip()
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        action = str(obj.get("action", ""))
        root = str(obj.get("root", ""))
        node = str(obj.get("node", ""))
        # Case 4 backend validation: ignore front-end as a candidate.
        if node and node != root and node != "front-end" and action.startswith("candidate_"):
            seen.add(node)
print(len(seen))
print(",".join(sorted(seen)))
PY
}

max_agent_dropped() {
  local file="$1"
  python3 - "$file" <<'PY'
import re
import sys

path = sys.argv[1]
mx = 0
with open(path, "r", encoding="utf-8", errors="ignore") as f:
    for line in f:
        for m in re.finditer(r"agent_dropped=(\d+)", line):
            try:
                mx = max(mx, int(m.group(1)))
            except Exception:
                pass
print(mx)
PY
}

count_non_timeout_loop_errors() {
  local file="$1"
  python3 - "$file" <<'PY'
import sys

path = sys.argv[1]
count = 0
with open(path, "r", encoding="utf-8", errors="ignore") as f:
    for line in f:
        if "Controller loop error" not in line:
            continue
        lower = line.lower()
        if "timeout" in lower or "timed out" in lower:
            continue
        count += 1
print(count)
PY
}

count_zero_rps_scale_actions() {
  local file="$1"
  python3 - "$file" <<'PY'
import json
import sys

path = sys.argv[1]
count = 0
with open(path, "r", encoding="utf-8", errors="ignore") as f:
    for line in f:
        if "TRACE " not in line:
            continue
        payload = line.split("TRACE ", 1)[1].strip()
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        action = str(obj.get("action", ""))
        if not action.startswith("scale_to_"):
            continue
        try:
            rps = float(obj.get("rps", 0.0))
        except Exception:
            rps = 0.0
        if rps <= 0.0:
            count += 1
print(count)
PY
}

summarize_case() {
  local case_name="$1"
  local run_log="$OUT_DIR/${case_name}_${TS}.run.log"
  local ctrl_log="$OUT_DIR/${case_name}_${TS}.ctrl.log"
  local agg_log="$OUT_DIR/${case_name}_${TS}.agg.log"
  local breaches_csv="$OUT_DIR/${case_name}_${TS}.breaches.csv"
  local breaches=0 scale_fe scale_orders panic_root skip_ew root_veto loop_err loop_err_non_timeout max_dropped zero_rps_scale

  if [[ -f "$breaches_csv" ]]; then
    breaches=$(( $(wc -l < "$breaches_csv") - 1 ))
    if (( breaches < 0 )); then breaches=0; fi
  fi

  scale_fe=$(count_or_zero "SCALING front-end" "$ctrl_log")
  scale_orders=$(count_or_zero "SCALING orders" "$ctrl_log")
  panic_root=$(count_or_zero "PANIC root trigger" "$ctrl_log")
  skip_ew=$(count_or_zero "skip_likely_external_wait" "$ctrl_log")
  root_veto=$(count_or_zero "root_without_local_evidence|skip_root_pain_without_local_evidence" "$ctrl_log")
  loop_err=$(count_or_zero "Controller loop error" "$ctrl_log")
  loop_err_non_timeout=0
  if [[ -f "$ctrl_log" ]]; then
    loop_err_non_timeout="$(count_non_timeout_loop_errors "$ctrl_log")"
  fi
  zero_rps_scale=0
  if [[ -f "$ctrl_log" ]]; then
    zero_rps_scale="$(count_zero_rps_scale_actions "$ctrl_log")"
  fi
  max_dropped=0
  if [[ -f "$agg_log" ]]; then
    max_dropped="$(max_agent_dropped "$agg_log")"
  fi

  echo "=== ${case_name} ==="
  [[ -f "$run_log" ]] && grep -E "^\\[start\\]|^\\[done\\]" "$run_log" || true
  echo "breaches=${breaches} scaleFE=${scale_fe} scaleORD=${scale_orders} panicRoot=${panic_root} skipEW=${skip_ew} rootVeto=${root_veto} loopErr=${loop_err} loopErrNonTimeout=${loop_err_non_timeout} maxAgentDropped=${max_dropped} zeroRpsScaleActions=${zero_rps_scale}"
  if (( max_dropped > 0 )); then
    echo "warn=agent_dropped_detected (max=${max_dropped})"
  fi

  if [[ "$case_name" == "case4_dual_hot" ]]; then
    local trace_stats non_root_candidate_count non_root_nodes
    trace_stats="$(summarize_trace_nodes "$ctrl_log")"
    non_root_candidate_count="$(echo "$trace_stats" | sed -n '1p')"
    non_root_nodes="$(echo "$trace_stats" | sed -n '2p')"
    echo "nonRootCandidateNodes=${non_root_candidate_count} nodes=${non_root_nodes:-<none>}"
  fi

  case "$case_name" in
    case1_frontend)
      if (( scale_fe >= 1 )) && (( zero_rps_scale == 0 )) && (( loop_err_non_timeout == 0 )); then
        echo "result=PASS (front-end scale with no zero-RPS victim)"
      else
        echo "result=FAIL (expected front-end actuation and no zero-RPS victim)"
      fi
      ;;
    case2_external_wait)
      if (( skip_ew > 0 )) && (( scale_fe <= 2 )) && (( loop_err_non_timeout == 0 )); then
        echo "result=PASS (external-wait deflection visible)"
      else
        echo "result=FAIL (expected skip_likely_external_wait and no aggressive FE scaling)"
      fi
      ;;
    case3_no_panic)
      if (( panic_root == 0 )) && (( scale_fe == 0 )) && (( loop_err_non_timeout == 0 )); then
        echo "result=PASS (no panic/no scale)"
      else
        echo "result=FAIL (unexpected panic/scale)"
      fi
      ;;
    case4_dual_hot)
      local trace_stats non_root_candidate_count
      trace_stats="$(summarize_trace_nodes "$ctrl_log")"
      non_root_candidate_count="$(echo "$trace_stats" | sed -n '1p')"
      if (( non_root_candidate_count >= 2 )) && (( loop_err_non_timeout == 0 )); then
        echo "result=PASS (>=2 backend candidates observed)"
      else
        echo "result=FAIL (insufficient multi-backend evidence)"
      fi
      ;;
  esac
  echo
}

if [[ ! -f "$CASE4_MIX" ]]; then
  echo "[error] CASE4_MIX not found: $CASE4_MIX"
  echo "        Provide CASE4_MIX=... that stresses two backend services."
  exit 1
fi

echo "[info] TS=$TS"
echo "[info] FRONTEND_URL=$FRONTEND_URL"
echo "[info] AGGREGATOR_URL=$AGGREGATOR_URL"
echo "[info] OUT_DIR=$OUT_DIR"

run_case case1_frontend \
  --profile sockshop \
  --mix-file deploy/03-evaluation/workloads/sockshop-catalogue-proof.yaml \
  --mode sudden-burst \
  --duration 120 \
  --warmup-seconds 10 \
  --base-rps 5 \
  --burst-rps 200 \
  --burst-at 20 \
  --sample-interval 2

run_case case2_external_wait \
  --profile sockshop \
  --mix-file deploy/03-evaluation/workloads/sockshop-ew-mix.yaml \
  --mode sudden-burst \
  --duration 120 \
  --warmup-seconds 10 \
  --base-rps 5 \
  --burst-rps 200 \
  --burst-at 20 \
  --sample-interval 2

run_case case3_no_panic \
  --profile generic \
  --mode steady \
  --duration 90 \
  --warmup-seconds 10 \
  --base-rps 2 \
  --burst-rps 2 \
  --burst-at 20 \
  --sample-interval 2

run_case case4_dual_hot \
  --profile sockshop \
  --mix-file "$CASE4_MIX" \
  --mode sudden-burst \
  --duration 120 \
  --warmup-seconds 10 \
  --base-rps 5 \
  --burst-rps 200 \
  --burst-at 20 \
  --sample-interval 2

summarize_case case1_frontend
summarize_case case2_external_wait
summarize_case case3_no_panic
summarize_case case4_dual_hot

echo "[done] artifacts in $OUT_DIR with timestamp $TS"
