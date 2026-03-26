#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${ROOT_DIR}/../../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
AGG_URL="${AGG_URL:-http://127.0.0.1:30938}"
OUTPUT_ROOT="${REPO_ROOT}/results/scenario_autoscaling/sockshop"
CASE_DIR="${ROOT_DIR}/cases"

mkdir -p "${OUTPUT_ROOT}"

selection="${1:-all}"
arm_selection="${2:-all}"

case_paths=()
if [[ "${selection}" == "all" ]]; then
  for case_path in "${CASE_DIR}"/*.json; do
    case_paths+=("${case_path}")
  done
else
  case_paths+=("${CASE_DIR}/${selection}.json")
fi

arms=()
if [[ "${arm_selection}" == "all" ]]; then
  arms=(noautoscale hpa50 hpa70 thrivescale)
else
  arms=("${arm_selection}")
fi

for arm in "${arms[@]}"; do
  for case_path in "${case_paths[@]}"; do
    echo
    echo "===== AUTOSCALER SCENARIO: ${arm} :: $(basename "${case_path}" .json) ====="
    "${PYTHON_BIN}" "${ROOT_DIR}/run_autoscaler_scenario_case.py" \
      --case-config "${case_path}" \
      --output-root "${OUTPUT_ROOT}" \
      --arm "${arm}" \
      --aggregator-base-url "${AGG_URL}"
  done
done

"${PYTHON_BIN}" "${ROOT_DIR}/summarize_autoscaler_scenario_phase.py" \
  --results-root "${OUTPUT_ROOT}"
