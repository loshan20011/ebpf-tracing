#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
BENCH_DIR="${ROOT_DIR}/src/scripts/benchmark"
COMMON_DIR="${ROOT_DIR}/src/scripts/common"
RESULTS_DIR="${ROOT_DIR}/results/benchmark/login"
BASE_URL="${BASE_URL:-http://127.0.0.1:30001}"
NAMESPACE="${NAMESPACE:-sock-shop}"
SYSTEM_NAMESPACE="${SYSTEM_NAMESPACE:-thrive-scale}"
CREDS_FILE="${RESULTS_DIR}/login_users.ndjson"
DEFAULT_WORKLOAD_FILE="${RESULTS_DIR}/frozen_workload.json"
FALLBACK_STATE_FILE="${RESULTS_DIR}/frozen_workload_state.json"
PROBE_RUNNER="${BENCH_DIR}/run_simple_login_benchmark.py"
SEED_SCRIPT="${BENCH_DIR}/seed_login_users.py"
FALLBACK_SCALE_FACTOR="${FALLBACK_SCALE_FACTOR:-0.75}"
STABILIZE_SECONDS="${STABILIZE_SECONDS:-75}"
WINDOW_SECONDS="${WINDOW_SECONDS:-10}"

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "missing required command: $1" >&2; exit 1; }
}

init_layout() {
  mkdir -p "${RESULTS_DIR}/hpa50" "${RESULTS_DIR}/hpa75" "${RESULTS_DIR}/thrivescale"
  if [[ ! -f "${DEFAULT_WORKLOAD_FILE}" ]]; then
    cat > "${DEFAULT_WORKLOAD_FILE}" <<'EOF'
[
  {"name":"phase_a","duration_seconds":120,"rps":100},
  {"name":"phase_b","duration_seconds":180,"rps":150},
  {"name":"phase_c","duration_seconds":240,"rps":250},
  {"name":"phase_d","duration_seconds":180,"rps":175},
  {"name":"phase_e","duration_seconds":180,"rps":325}
]
EOF
  fi
  if [[ ! -f "${FALLBACK_STATE_FILE}" ]]; then
    cat > "${FALLBACK_STATE_FILE}" <<'EOF'
{"fallback_applied": false, "scale_factor": 1.0}
EOF
  fi
}

kubectl_wait_stable() {
  local timeout_secs="${1:-600}"
  python3 - "$timeout_secs" "$NAMESPACE" <<'PY'
import json
import shutil
import subprocess
import sys
import time

timeout_secs = int(sys.argv[1])
namespace = sys.argv[2]
required = ["front-end", "user", "carts", "user-db", "carts-db"]

def kubectl(args):
    kubectl = shutil.which("kubectl")
    if kubectl:
        cmd = [kubectl, *args]
    else:
        k3s = shutil.which("k3s")
        if not k3s:
            raise SystemExit("kubectl or k3s not found")
        cmd = [k3s, "kubectl", *args]
    out = subprocess.check_output(cmd, text=True)
    return json.loads(out)

def collect_restarts(pods):
    result = {}
    for pod in pods.get("items", []):
        pod_name = pod.get("metadata", {}).get("name", "")
        if not any(pod_name.startswith(prefix) for prefix in required):
            continue
        total = 0
        for cs in pod.get("status", {}).get("containerStatuses", []) or []:
            total += int(cs.get("restartCount", 0) or 0)
        result[pod_name] = total
    return result

deadline = time.time() + timeout_secs
while time.time() < deadline:
    deploys = kubectl(["get", "deploy", "-n", namespace, "-o", "json"])
    pods = kubectl(["get", "pods", "-n", namespace, "-o", "json"])
    deploy_map = {item["metadata"]["name"]: item for item in deploys.get("items", [])}
    ok = True
    for name in required:
        item = deploy_map.get(name)
        if not item:
            ok = False
            break
        desired = int(item.get("spec", {}).get("replicas", 0) or 0)
        available = int(item.get("status", {}).get("availableReplicas", 0) or 0)
        updated = int(item.get("status", {}).get("updatedReplicas", 0) or 0)
        total = int(item.get("status", {}).get("replicas", 0) or 0)
        unavailable = int(item.get("status", {}).get("unavailableReplicas", 0) or 0)
        if available < desired or updated < desired or total != desired or unavailable > 0:
            ok = False
            break
    if ok:
        for pod in pods.get("items", []):
            pod_name = pod.get("metadata", {}).get("name", "")
            if not any(pod_name.startswith(prefix) for prefix in required):
                continue
            phase = str(pod.get("status", {}).get("phase", ""))
            if phase == "Pending":
                ok = False
                break
            for cs in pod.get("status", {}).get("containerStatuses", []) or []:
                waiting = ((cs.get("state", {}) or {}).get("waiting", {}) or {})
                if waiting.get("reason") in {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull"}:
                    ok = False
                    break
            if not ok:
                break
    if ok:
        first = collect_restarts(pods)
        time.sleep(15)
        pods_again = kubectl(["get", "pods", "-n", namespace, "-o", "json"])
        second = collect_restarts(pods_again)
        if all(second.get(name, 0) <= count for name, count in first.items()):
            print("stable")
            raise SystemExit(0)
    time.sleep(5)
raise SystemExit("environment not stable")
PY
}

http_wait_ready() {
  local timeout_secs="${1:-180}"
  python3 - "$timeout_secs" "$BASE_URL" <<'PY'
import sys
import time
import urllib.request

timeout_secs = int(sys.argv[1])
base_url = sys.argv[2].rstrip("/") + "/"
deadline = time.time() + timeout_secs
while time.time() < deadline:
    try:
        with urllib.request.urlopen(base_url, timeout=10) as resp:
            if int(resp.status or 0) == 200:
                print("http-ready")
                raise SystemExit(0)
    except Exception:
        pass
    time.sleep(2)
raise SystemExit("front-end HTTP not ready")
PY
}

apply_fixed_resources() {
  kubectl -n "${NAMESPACE}" set resources deployment/front-end --requests=cpu=100m,memory=128Mi --limits=cpu=500m,memory=256Mi
  kubectl -n "${NAMESPACE}" set resources deployment/user --requests=cpu=100m,memory=128Mi --limits=cpu=300m,memory=256Mi
  kubectl -n "${NAMESPACE}" set resources deployment/carts --requests=cpu=300m,memory=512Mi --limits=cpu=600m,memory=1Gi
}

restore_min_replicas() {
  kubectl -n "${NAMESPACE}" scale deployment/front-end --replicas=1
  kubectl -n "${NAMESPACE}" scale deployment/user --replicas=1
  kubectl -n "${NAMESPACE}" scale deployment/carts --replicas=1
}

apply_thrivescale_slos() {
  python3 - "${COMMON_DIR}" <<'PY'
import json
import sys
sys.path.insert(0, sys.argv[1])
from patch_demo_gateway_slo import prepare_sock_shop_slos

case_cfg = {
    "replica_bounds": {"minReplicas": 1, "maxReplicas": 13},
    "service_slos": {
        "sockshop-front-end-slo": {"targetDeployment": "front-end", "sloLatency": 150.0, "minReplicas": 1, "maxReplicas": 10, "priority": "primary"},
        "sockshop-user-slo": {"targetDeployment": "user", "sloLatency": 100.0, "minReplicas": 1, "maxReplicas": 13, "priority": "secondary"},
        "sockshop-carts-slo": {"targetDeployment": "carts", "sloLatency": 100.0, "minReplicas": 1, "maxReplicas": 13, "priority": "secondary"}
    }
}
result = prepare_sock_shop_slos(case_cfg, namespace="sock-shop")
print(json.dumps(result, indent=2, sort_keys=True))
if not result.get("ok"):
    raise SystemExit(1)
PY
}

delete_hpas() {
  kubectl -n "${NAMESPACE}" delete hpa front-end user carts --ignore-not-found
}

apply_hpa_profile() {
  local cpu_target="$1"
  delete_hpas
  kubectl -n "${NAMESPACE}" autoscale deployment front-end --cpu-percent="${cpu_target}" --min=1 --max=10
  kubectl -n "${NAMESPACE}" autoscale deployment user --cpu-percent="${cpu_target}" --min=1 --max=13
  kubectl -n "${NAMESPACE}" autoscale deployment carts --cpu-percent="${cpu_target}" --min=1 --max=13
}

disable_thrivescale_control() {
  kubectl -n "${SYSTEM_NAMESPACE}" scale deployment/custom-autoscaler --replicas=0 || true
}

clear_thrivescale_state() {
  kubectl -n "${SYSTEM_NAMESPACE}" exec deploy/redis -- redis-cli FLUSHDB >/dev/null
}

enable_thrivescale_control() {
  kubectl -n "${SYSTEM_NAMESPACE}" scale deployment/aggregator --replicas=1 || true
  kubectl -n "${SYSTEM_NAMESPACE}" scale deployment/custom-autoscaler --replicas=1 || true
  clear_thrivescale_state
  kubectl -n "${SYSTEM_NAMESPACE}" rollout restart deployment/aggregator
  kubectl -n "${SYSTEM_NAMESPACE}" rollout restart deployment/custom-autoscaler
  kubectl -n "${SYSTEM_NAMESPACE}" rollout status deployment/aggregator --timeout=300s
  kubectl -n "${SYSTEM_NAMESPACE}" rollout status deployment/custom-autoscaler --timeout=300s
}

seed_users() {
  local seed_prefix="benchmark_login_$(date +%s)"
  python3 "${SEED_SCRIPT}" \
    --base-url "${BASE_URL}" \
    --count 100 \
    --retries 8 \
    --retry-delay-ms 500 \
    --prefix "${seed_prefix}" \
    --output "${CREDS_FILE}"
}

verify_manual_login() {
  python3 - "${CREDS_FILE}" "${BASE_URL}" <<'PY'
import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

creds_file = Path(sys.argv[1])
base_url = sys.argv[2].rstrip("/") + "/login"
lines = [line.strip() for line in creds_file.read_text(encoding="utf-8").splitlines() if line.strip()]
if not lines:
    raise SystemExit("no seeded users found")
rec = json.loads(lines[0])
token = base64.b64encode(f"{rec['username']}:{rec['password']}".encode("utf-8")).decode("ascii")
req = urllib.request.Request(base_url, headers={"Authorization": f"Basic {token}", "User-Agent": "final-benchmark-manual-login/1.0"})
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        if int(resp.status or 0) != 200:
            raise SystemExit(f"/login returned {int(resp.status or 0)}")
except urllib.error.HTTPError as exc:
    raise SystemExit(f"/login returned {int(exc.code or 0)}")
print("login-ok")
PY
}

verify_carts_health() {
  python3 - "$NAMESPACE" <<'PY'
import json
import shutil
import subprocess
import sys

namespace = sys.argv[1]

def cmd(args):
    kubectl = shutil.which("kubectl")
    if kubectl:
        return [kubectl, *args]
    k3s = shutil.which("k3s")
    if k3s:
        return [k3s, "kubectl", *args]
    raise SystemExit("kubectl or k3s not found")

pods = json.loads(subprocess.check_output(cmd(["get", "pods", "-n", namespace, "-o", "json"]), text=True))
carts = []
for pod in pods.get("items", []):
    name = str(pod.get("metadata", {}).get("name", ""))
    if name.startswith("carts-") and not name.startswith("carts-db-"):
        carts.append(name)
if not carts:
    raise SystemExit("no carts pods found")

for pod_name in carts:
    probe = subprocess.run(
        cmd(["exec", "-n", namespace, pod_name, "--", "sh", "-c", "wget -qO- http://127.0.0.1:8080/health || curl -fsS http://127.0.0.1:8080/health"]),
        text=True,
        capture_output=True,
        timeout=30,
    )
    if probe.returncode != 0:
        raise SystemExit(f"carts health endpoint failed for {pod_name}: {probe.stderr.strip() or probe.stdout.strip()}")
print("carts-health-ok")
PY
}

capture_carts_idle_state() {
  local out_file="$1"
  python3 - "$NAMESPACE" "$out_file" <<'PY'
import json
import shutil
import subprocess
import sys
from pathlib import Path

namespace = sys.argv[1]
out_file = Path(sys.argv[2])

def cmd(args):
    kubectl = shutil.which("kubectl")
    if kubectl:
        return [kubectl, *args]
    k3s = shutil.which("k3s")
    if k3s:
        return [k3s, "kubectl", *args]
    raise SystemExit("kubectl or k3s not found")

pods = json.loads(subprocess.check_output(cmd(["get", "pods", "-n", namespace, "-o", "json"]), text=True))
rows = []
for pod in pods.get("items", []):
    name = str(pod.get("metadata", {}).get("name", ""))
    if not name.startswith("carts-"):
        continue
    restarts = 0
    ready = True
    last_terminated_reasons = []
    for cs in pod.get("status", {}).get("containerStatuses", []) or []:
        restarts += int(cs.get("restartCount", 0) or 0)
        ready = ready and bool(cs.get("ready", False))
        terminated = ((cs.get("lastState", {}) or {}).get("terminated", {}) or {})
        if terminated.get("reason"):
            last_terminated_reasons.append(str(terminated["reason"]))
    rows.append({"name": name, "ready": ready, "restart_count": restarts, "last_terminated_reasons": last_terminated_reasons})
out_file.write_text(json.dumps(rows, indent=2, sort_keys=True), encoding="utf-8")
print("carts-idle-state-saved")
PY
}

verify_carts_idle_stability() {
  local before_file="$1"
  local after_file="$2"
  python3 - "$before_file" "$after_file" <<'PY'
import json
import sys

before = {row["name"]: row for row in json.loads(open(sys.argv[1], encoding="utf-8").read())}
after = {row["name"]: row for row in json.loads(open(sys.argv[2], encoding="utf-8").read())}
if not after:
    raise SystemExit("no carts pods found after stabilization")
for name, row in after.items():
    if not bool(row.get("ready", False)):
        raise SystemExit(f"carts pod not ready after stabilization: {name}")
    prev = before.get(name)
    if prev and int(row.get("restart_count", 0) or 0) > int(prev.get("restart_count", 0) or 0):
        raise SystemExit(f"carts restarted during idle stabilization: {name}")
print("carts-idle-stable")
PY
}

write_static_configs() {
  local arm_dir="$1"
  local arm="$2"
  cat > "${arm_dir}/resource_config.json" <<'EOF'
{
  "front-end": {"cpu_request": "100m", "cpu_limit": "500m", "memory_request": "128Mi", "memory_limit": "256Mi"},
  "user": {"cpu_request": "100m", "cpu_limit": "300m", "memory_request": "128Mi", "memory_limit": "256Mi"},
  "carts": {"cpu_request": "300m", "cpu_limit": "600m", "memory_request": "512Mi", "memory_limit": "1Gi"}
}
EOF
  cat > "${arm_dir}/replica_bounds.json" <<'EOF'
{
  "front-end": {"min": 1, "max": 10},
  "user": {"min": 1, "max": 13},
  "carts": {"min": 1, "max": 13}
}
EOF
  cat > "${arm_dir}/slo_config.json" <<'EOF'
{
  "client_p90_ms": 150.0
}
EOF
  cat > "${arm_dir}/autoscaler_mode.json" <<EOF
{
  "arm": "${arm}"
}
EOF
}

prepare_arm() {
  local arm="$1"
  local idle_before="${RESULTS_DIR}/.carts_idle_before_${arm}.json"
  local idle_after="${RESULTS_DIR}/.carts_idle_after_${arm}.json"
  init_layout
  apply_fixed_resources
  restore_min_replicas
  case "${arm}" in
    hpa50)
      disable_thrivescale_control
      apply_hpa_profile 50
      ;;
    hpa75)
      disable_thrivescale_control
      apply_hpa_profile 75
      ;;
    thrivescale)
      delete_hpas
      apply_thrivescale_slos
      enable_thrivescale_control
      ;;
    *)
      echo "unknown arm: ${arm}" >&2
      exit 1
      ;;
  esac
  http_wait_ready 180
  kubectl_wait_stable 600
  verify_carts_health
  seed_users
  verify_manual_login
  capture_carts_idle_state "${idle_before}"
  sleep "${STABILIZE_SECONDS}"
  capture_carts_idle_state "${idle_after}"
  verify_carts_idle_stability "${idle_before}" "${idle_after}"
}

prepare_neutral() {
  init_layout
  apply_fixed_resources
  restore_min_replicas
  delete_hpas
  disable_thrivescale_control
  http_wait_ready 180
  kubectl_wait_stable 600
  verify_carts_health
  seed_users
  verify_manual_login
  sleep "${STABILIZE_SECONDS}"
}

maybe_apply_workload_fallback() {
  local arm="$1"
  local arm_dir="$2"
  python3 - "${DEFAULT_WORKLOAD_FILE}" "${FALLBACK_STATE_FILE}" "${arm_dir}/summary.json" "${FALLBACK_SCALE_FACTOR}" <<'PY'
import json
import sys
from pathlib import Path

workload_file = Path(sys.argv[1])
state_file = Path(sys.argv[2])
summary_file = Path(sys.argv[3])
scale_factor = float(sys.argv[4])
state = json.loads(state_file.read_text(encoding="utf-8"))
summary = json.loads(summary_file.read_text(encoding="utf-8"))
if bool(state.get("fallback_applied", False)):
    raise SystemExit(0)
if not bool(summary.get("workload_fallback_recommended", False)):
    raise SystemExit(0)
phases = json.loads(workload_file.read_text(encoding="utf-8"))
scaled = []
for phase in phases:
    scaled.append(
        {
            "name": phase["name"],
            "duration_seconds": int(phase["duration_seconds"]),
            "rps": max(1, int(round(float(phase["rps"]) * scale_factor))),
        }
    )
workload_file.write_text(json.dumps(scaled, indent=2, sort_keys=True), encoding="utf-8")
state_file.write_text(json.dumps({"fallback_applied": True, "scale_factor": scale_factor}, indent=2, sort_keys=True), encoding="utf-8")
print("fallback-applied")
PY
}

run_arm() {
  local arm="$1"
  local arm_dir="${RESULTS_DIR}/${arm}"
  prepare_arm "${arm}"
  rm -rf "${arm_dir}"
  mkdir -p "${arm_dir}"
  write_static_configs "${arm_dir}" "${arm}"
  python3 "${PROBE_RUNNER}" \
    --namespace "${NAMESPACE}" \
    --base-url "${BASE_URL}" \
    --creds-file "${CREDS_FILE}" \
    --output-dir "${arm_dir}" \
    --phases-file "${DEFAULT_WORKLOAD_FILE}" \
    --slo-ms 150 \
    --stable-timeout-seconds 600 \
    --sample-interval-seconds "${WINDOW_SECONDS}" \
    --window-seconds "${WINDOW_SECONDS}" \
        --arm-name "${arm}"
  python3 - "${arm_dir}/summary.json" <<'PY'
import json, sys
summary = json.loads(open(sys.argv[1], encoding="utf-8").read())
carts = summary.get("carts_validity", {})
if carts.get("run_invalid"):
    raise SystemExit(f"invalid benchmark run due to carts validity: {carts.get('reason', 'unknown')}")
PY
  if [[ "${arm}" == "hpa50" ]]; then
    if python3 - "${arm_dir}/summary.json" <<'PY'
import json, sys
summary = json.loads(open(sys.argv[1], encoding="utf-8").read())
raise SystemExit(0 if summary.get("workload_fallback_recommended") else 1)
PY
    then
      maybe_apply_workload_fallback "${arm}" "${arm_dir}" || true
      rm -rf "${arm_dir}"
      mkdir -p "${arm_dir}"
      write_static_configs "${arm_dir}" "${arm}"
      prepare_arm "${arm}"
      python3 "${PROBE_RUNNER}" \
        --namespace "${NAMESPACE}" \
        --base-url "${BASE_URL}" \
        --creds-file "${CREDS_FILE}" \
        --output-dir "${arm_dir}" \
        --phases-file "${DEFAULT_WORKLOAD_FILE}" \
        --slo-ms 150 \
        --stable-timeout-seconds 600 \
        --sample-interval-seconds "${WINDOW_SECONDS}" \
        --window-seconds "${WINDOW_SECONDS}" \
        --arm-name "${arm}"
      python3 - "${arm_dir}/summary.json" <<'PY'
import json, sys
summary = json.loads(open(sys.argv[1], encoding="utf-8").read())
carts = summary.get("carts_validity", {})
if carts.get("run_invalid"):
    raise SystemExit(f"invalid benchmark run due to carts validity: {carts.get('reason', 'unknown')}")
PY
    fi
  fi
}

compare_arms() {
  python3 - "${RESULTS_DIR}" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
arms = [("hpa50", "HPA-50"), ("hpa75", "HPA-75"), ("thrivescale", "ThriveScale")]
rows = []
for folder, label in arms:
    summary_path = root / folder / "summary.json"
    if not summary_path.exists():
        continue
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    rows.append(
        {
            "Arm": label,
            "SLO violation time": summary["benchmark_metrics"]["slo_violation_time_seconds"],
            "SLO violation rate": summary["benchmark_metrics"]["slo_violation_rate"],
            "Time to first action": summary["benchmark_metrics"]["time_to_first_action_seconds"],
            "Recovery time": summary["benchmark_metrics"]["recovery_time_seconds"],
            "Peak replicas": summary["replica_metrics"]["overall_peak_replicas"],
            "Average replicas": summary["replica_metrics"]["overall_avg_replicas"],
            "Requested CPU core-minutes": summary["replica_metrics"]["total_requested_cpu_core_minutes"],
            "Replica-seconds": summary["replica_metrics"]["total_replica_seconds"],
            "Error rate": summary["frontend_client_metrics"]["error_rate"],
        }
    )
    (root / folder / "exact_workload_profile.json").write_text(json.dumps(summary["phases"], indent=2, sort_keys=True), encoding="utf-8")
if not rows:
    raise SystemExit("no completed arm summaries found")

lines = [
    "# Final Sock Shop Benchmark Comparison",
    "",
    "| Arm | SLO violation time | SLO violation rate | Time to first action | Recovery time | Peak replicas | Average replicas | Requested CPU core-minutes | Replica-seconds | Error rate |",
    "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
]
for row in rows:
    lines.append(
        f"| {row['Arm']} | {row['SLO violation time']} | {row['SLO violation rate']} | "
        f"{row['Time to first action']} | {row['Recovery time']} | {row['Peak replicas']} | "
        f"{row['Average replicas']} | {row['Requested CPU core-minutes']} | {row['Replica-seconds']} | {row['Error rate']} |"
    )

(root / "final_comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
(root / "final_comparison.json").write_text(json.dumps(rows, indent=2, sort_keys=True), encoding="utf-8")
print(json.dumps(rows, indent=2, sort_keys=True))
PY
}

show_usage() {
  cat <<EOF
Usage:
  $(basename "$0") prepare
  $(basename "$0") verify
  $(basename "$0") run-arm <hpa50|hpa75|thrivescale>
  $(basename "$0") compare

Outputs:
  frozen workload: ${DEFAULT_WORKLOAD_FILE}
  workload state:  ${FALLBACK_STATE_FILE}
  hpa50 results:   ${RESULTS_DIR}/hpa50
  hpa75 results:   ${RESULTS_DIR}/hpa75
  thrive results:  ${RESULTS_DIR}/thrivescale
EOF
}

require_cmd python3
require_cmd kubectl

case "${1:-}" in
  prepare)
    prepare_neutral
    ;;
  verify)
    http_wait_ready 180
    kubectl_wait_stable 600
    verify_manual_login
    ;;
  run-arm)
    run_arm "${2:-}"
    ;;
  compare)
    compare_arms
    ;;
  *)
    show_usage
    ;;
esac
