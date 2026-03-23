#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${ROOT_DIR}/../../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
AGG_URL="${AGG_URL:-http://127.0.0.1:30938}"
OUTPUT_ROOT="${REPO_ROOT}/results/replica_count/control_loop"
CASE_DIR="${ROOT_DIR}/cases"

run_case() {
  local case_name="$1"
  local case_path="${CASE_DIR}/${case_name}.json"
  echo
  echo "===== CASE: ${case_name} ====="
  "${PYTHON_BIN}" "${ROOT_DIR}/run_control_loop_case.py" \
    --case-config "${case_path}" \
    --output-root "${OUTPUT_ROOT}" \
    --aggregator-base-url "${AGG_URL}" \
    --mode control
}

selection="${1:-all}"

mkdir -p "${OUTPUT_ROOT}"
echo
echo "===== PHASE: control_loop (control) ====="

case "${selection}" in
  CL1_short_spike|CL2_sustained_increase|CL3_rise_then_recovery|CL4_bursty_repeated_spikes|CL5_downstream_sustained_bottleneck)
    run_case "${selection}"
    ;;
  all)
    for case_path in "${CASE_DIR}"/*.json; do
      run_case "$(basename "${case_path}" .json)"
    done
    ;;
  *)
    echo "Usage: $0 [all|CL1_short_spike|CL2_sustained_increase|CL3_rise_then_recovery|CL4_bursty_repeated_spikes|CL5_downstream_sustained_bottleneck]" >&2
    exit 1
    ;;
esac

"${PYTHON_BIN}" "${ROOT_DIR}/summarize_control_loop_phase.py" \
  --results-root "${OUTPUT_ROOT}"
