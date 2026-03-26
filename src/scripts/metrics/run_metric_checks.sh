#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
OUTPUT_ROOT="${OUTPUT_ROOT:-results/metrics}"

run_case() {
  local config_name="$1"
  local result_name="$2"
  echo
  echo "===== METRIC CHECK: ${result_name} ====="
  "${PYTHON_BIN}" "${ROOT_DIR}/run_functional_case.py" \
    --config "${ROOT_DIR}/cases/${config_name}.json" \
    --output-root "${OUTPUT_ROOT}"
  "${PYTHON_BIN}" "${ROOT_DIR}/summarize_functional_case.py" \
    --case-dir "${OUTPUT_ROOT}/${result_name}"
}

selection="${1:-all}"

case "${selection}" in
  all)
    run_case "baseline_low_steady" "baseline_low_steady"
    run_case "catalogue_only_low" "F1_graph_catalogue"
    run_case "cart_get_only_low" "F2_graph_cart"
    run_case "customers_only_low" "F3_graph_customers"
    ;;
  baseline|baseline_low_steady)
    run_case "baseline_low_steady" "baseline_low_steady"
    ;;
  catalogue|catalogue_only_low)
    run_case "catalogue_only_low" "F1_graph_catalogue"
    ;;
  cart_get|cart_get_only_low)
    run_case "cart_get_only_low" "F2_graph_cart"
    ;;
  customers|customers_only_low)
    run_case "customers_only_low" "F3_graph_customers"
    ;;
  *)
    echo "Unknown metric check selection: ${selection}" >&2
    echo "Use one of: all, baseline, catalogue, cart_get, customers" >&2
    exit 1
    ;;
esac
