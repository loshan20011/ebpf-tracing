#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${ROOT_DIR}/../../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
OUTPUT_ROOT="${OUTPUT_ROOT:-${REPO_ROOT}/results/metrics}"
CASE_DIR="${ROOT_DIR}/cases"

selection="${1:-all}"

run_case() {
  local config_name="$1"
  local result_name="$2"
  echo
  echo "===== GRAPH CHECK: ${result_name} ====="
  "${PYTHON_BIN}" "${ROOT_DIR}/run_functional_case.py" \
    --config "${CASE_DIR}/${config_name}.json" \
    --output-root "${OUTPUT_ROOT}"
  "${PYTHON_BIN}" "${ROOT_DIR}/summarize_functional_case.py" \
    --case-dir "${OUTPUT_ROOT}/${result_name}"
}

case "${selection}" in
  all)
    run_case "catalogue_only_low" "F1_graph_catalogue"
    run_case "login_only_low" "F2_graph_login"
    run_case "cart_get_only_low" "F3_graph_cart"
    run_case "customers_only_low" "F4_graph_customers"
    ;;
  catalogue|catalogue_only_low)
    run_case "catalogue_only_low" "F1_graph_catalogue"
    ;;
  login|login_only_low)
    run_case "login_only_low" "F2_graph_login"
    ;;
  cart_get|cart_get_only_low)
    run_case "cart_get_only_low" "F3_graph_cart"
    ;;
  customers|customers_only_low)
    run_case "customers_only_low" "F4_graph_customers"
    ;;
  *)
    echo "Unknown graph check selection: ${selection}" >&2
    echo "Use one of: all, catalogue, login, cart_get, customers" >&2
    exit 1
    ;;
esac
