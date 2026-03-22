#!/usr/bin/env bash
set -euo pipefail

AGG="${AGG:-http://127.0.0.1:30938}"
DEMO_BASE="${DEMO_BASE:-http://172.31.32.23:80}"
SOCK_BASE="${SOCK_BASE:-http://172.31.32.23:30001}"
WAIT_AFTER_RESET_S="${WAIT_AFTER_RESET_S:-8}"
WAIT_AFTER_SWITCH_S="${WAIT_AFTER_SWITCH_S:-10}"
HEY_DURATION="${HEY_DURATION:-1m}"
HEY_CONCURRENCY="${HEY_CONCURRENCY:-1}"
HEY_QPS="${HEY_QPS:-10}"

switch_target() {
  local ns="$1"
  local root="$2"

  echo
  echo "===== SWITCH TARGET: namespace=${ns} root=${root} ====="

  sudo k3s kubectl -n thrive-scale set env deploy/aggregator TARGET_NAMESPACE="${ns}" >/dev/null
  sudo k3s kubectl -n thrive-scale set env deploy/custom-autoscaler TARGET_NAMESPACE="${ns}" ROOT_SERVICE="${root}" >/dev/null
  sudo k3s kubectl -n thrive-scale set env ds/bpf-agent TARGET_NAMESPACE="${ns}" >/dev/null

  sudo k3s kubectl -n thrive-scale rollout status deploy/aggregator --timeout=180s >/dev/null
  sudo k3s kubectl -n thrive-scale rollout status deploy/custom-autoscaler --timeout=180s >/dev/null
  sudo k3s kubectl -n thrive-scale rollout status ds/bpf-agent --timeout=180s >/dev/null

  echo "Target switched. Waiting ${WAIT_AFTER_SWITCH_S}s for steady state..."
  sleep "${WAIT_AFTER_SWITCH_S}"
}

disable_autoscaler() {
  echo
  echo "===== AUTOSCALER OFF ====="
  sudo k3s kubectl -n thrive-scale scale deploy/custom-autoscaler --replicas=0 >/dev/null
  sleep 5
}

enable_autoscaler() {
  echo
  echo "===== AUTOSCALER ON ====="
  sudo k3s kubectl -n thrive-scale scale deploy/custom-autoscaler --replicas=1 >/dev/null || true
  sudo k3s kubectl -n thrive-scale rollout status deploy/custom-autoscaler --timeout=180s >/dev/null || true
}

print_summary() {
  local label="$1"
  local route="$2"
  local hey_out="$3"
  local graph_json="$4"
  local root_metric="$5"
  local svc1="${6:-}"
  local svc2="${7:-}"
  local svc3="${8:-}"

  local hey_rps
  local hey_p90_ms
  hey_rps="$(echo "${hey_out}" | awk '/Requests\/sec:/ {print $2}')"
  hey_p90_ms="$(echo "${hey_out}" | awk '/90% in/ {printf "%.3f", $3 * 1000}')"

  echo "${hey_out}"
  echo
  echo "===== FINAL SUMMARY ====="
  echo "Case: ${label}"
  echo "Route: ${route}"
  echo "Client RPS: ${hey_rps:-N/A}"
  echo "Client p90 (ms): ${hey_p90_ms:-N/A}"
  echo

  echo "${graph_json}" | jq -r \
    --arg root "${root_metric}" \
    --arg s1 "${svc1}" \
    --arg s2 "${svc2}" \
    --arg s3 "${svc3}" '
      .metrics as $m |
      [
        "Topology: " + (
          [(.topology | to_entries[]? | .key as $src | .value[]? | "\($src) -> \(.)")] | join(", ")
        ),
        ""
      ] + (
        [$root, $s1, $s2, $s3]
        | map(select(length > 0))
        | map([
            . + ":",
            "  p90(ms): \($m[.].p90_latency)",
            "  truth_p90(ms): \($m[.].truth_p90_latency_ms)",
            "  rps: \($m[.].rps)",
            "  runq_p90(ms): \($m[.].runq_p90_latency)",
            ""
          ])
        | add
      )
      | .[]
    '
}

run_case() {
  local label="$1"
  local base_url="$2"
  local route="$3"
  local root_metric="$4"
  local svc1="${5:-}"
  local svc2="${6:-}"
  local svc3="${7:-}"

  echo
  echo "===================================================="
  echo "CASE: ${label}"
  echo "ROUTE: ${route}"
  echo "===================================================="

  curl -fsS "${AGG}/api/reset" >/dev/null
  sleep "${WAIT_AFTER_RESET_S}"

  local hey_out
  local graph_json
  hey_out="$(hey -z "${HEY_DURATION}" -c "${HEY_CONCURRENCY}" -q "${HEY_QPS}" "${base_url}${route}" 2>&1 || true)"
  graph_json="$(curl -fsS "${AGG}/api/graph")"

  print_summary "${label}" "${route}" "${hey_out}" "${graph_json}" "${root_metric}" "${svc1}" "${svc2}" "${svc3}"
}

run_demo_cases() {
  switch_target "thrive-demo" "gateway"
  disable_autoscaler
  run_case "demo_cpu" "${DEMO_BASE}" "/cpu?count=1500000" "gateway" "svc-cpu"
  run_case "demo_io" "${DEMO_BASE}" "/io" "gateway" "svc-io"
  run_case "demo_chain" "${DEMO_BASE}" "/chain?count=1500000" "gateway" "svc-chain" "svc-cpu"
  run_case "demo_fanout" "${DEMO_BASE}" "/fanout?count=1500000" "gateway" "svc-fanout" "svc-cpu" "svc-io"
}

run_sock_cases() {
  switch_target "sock-shop" "front-end"
  disable_autoscaler
  run_case "sock_catalogue" "${SOCK_BASE}" "/catalogue" "front-end" "catalogue"
  run_case "sock_customers" "${SOCK_BASE}" "/customers" "front-end" "user"
  run_case "sock_cart" "${SOCK_BASE}" "/cart" "front-end" "carts"
}

main() {
  local mode="${1:-all}"
  case "${mode}" in
    demo)
      run_demo_cases
      ;;
    sock|sock-shop)
      run_sock_cases
      ;;
    all)
      run_demo_cases
      run_sock_cases
      ;;
    *)
      echo "Usage: $0 [demo|sock|all]" >&2
      exit 1
      ;;
  esac

  enable_autoscaler
  echo
  echo "===== DONE ====="
}

main "$@"
