#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${ROOT_DIR}/../../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
AGG_URL="${AGG_URL:-http://127.0.0.1:30938}"

run_phase() {
  local phase_name="$1"
  local case_dir="$2"
  local output_root="$3"
  local mode="${4:-observation}"

  echo
  echo "===== PHASE: ${phase_name} (${mode}) ====="
  mkdir -p "${output_root}"

  local case
  for case in "${case_dir}"/*.json; do
    echo
    echo "===== CASE: $(basename "${case}" .json) ====="
    "${PYTHON_BIN}" "${ROOT_DIR}/run_bottleneck_case.py" \
      --case-config "${case}" \
      --output-root "${output_root}" \
      --aggregator-base-url "${AGG_URL}" \
      --mode "${mode}"
  done

  "${PYTHON_BIN}" "${ROOT_DIR}/summarize_bottleneck_phase.py" \
    --results-root "${output_root}"
}

selection="${1:-all}"
mode="${2:-observation}"

case "${selection}" in
  service)
    run_phase "bottleneck_service" "${ROOT_DIR}/bottleneck_service/cases" "${REPO_ROOT}/results/functional/bottleneck_service" "${mode}"
    ;;
  reason)
    run_phase "bottleneck_reason" "${ROOT_DIR}/bottleneck_reason/cases" "${REPO_ROOT}/results/functional/bottleneck_reason" "${mode}"
    ;;
  all)
    run_phase "bottleneck_service" "${ROOT_DIR}/bottleneck_service/cases" "${REPO_ROOT}/results/functional/bottleneck_service" "${mode}"
    run_phase "bottleneck_reason" "${ROOT_DIR}/bottleneck_reason/cases" "${REPO_ROOT}/results/functional/bottleneck_reason" "${mode}"
    ;;
  *)
    echo "Usage: $0 [service|reason|all] [observation|control]" >&2
    exit 1
    ;;
esac
