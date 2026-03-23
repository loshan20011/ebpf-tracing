#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${ROOT_DIR}/../../.." && pwd)"
COMMON_DIR="${REPO_ROOT}/src/scripts/common"
PYTHON_BIN="${PYTHON_BIN:-python3}"
AGG_URL="${AGG_URL:-http://127.0.0.1:30938}"
OUTPUT_ROOT="${REPO_ROOT}/results/bottleneck_service"
CASE_DIR="${ROOT_DIR}/cases"

mkdir -p "${OUTPUT_ROOT}"

selection="${1:-all}"
mode="${2:-observation}"

run_case() {
  local case_path="$1"
  echo
  echo "===== CASE: $(basename "${case_path}" .json) ====="
  "${PYTHON_BIN}" "${COMMON_DIR}/run_bottleneck_case.py" \
    --case-config "${case_path}" \
    --output-root "${OUTPUT_ROOT}" \
    --aggregator-base-url "${AGG_URL}" \
    --mode "${mode}"
}

case "${selection}" in
  all)
    for case_path in "${CASE_DIR}"/*.json; do
      run_case "${case_path}"
    done
    ;;
  *)
    run_case "${CASE_DIR}/${selection}.json"
    ;;
esac

"${PYTHON_BIN}" "${COMMON_DIR}/summarize_bottleneck_phase.py" \
  --results-root "${OUTPUT_ROOT}"
