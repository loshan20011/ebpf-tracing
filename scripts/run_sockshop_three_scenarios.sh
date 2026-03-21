#!/usr/bin/env bash
set -euo pipefail

APP_NS="${APP_NS:-sock-shop}"
CONTROL_NS="${CONTROL_NS:-thrive-scale}"
KUBECTL="${KUBECTL:-kubectl}"
RESULT_ROOT="${RESULT_ROOT:-results}"
PYTHON="${PYTHON:-python3}"

SCENARIO1_SLO="${SCENARIO1_SLO:-deploy/03-evaluation/scenario-front-end-slo.yaml}"
SCENARIO2_SLO="${SCENARIO2_SLO:-deploy/03-evaluation/scenario-catalogue-slo.yaml}"
SCENARIO3_SLO="${SCENARIO3_SLO:-deploy/03-evaluation/scenario-front-end-slo.yaml}"

SCENARIO1_HPA="${SCENARIO1_HPA:-deploy/03-evaluation/hpa-scenario3-frontend.yaml}"
SCENARIO2_HPA="${SCENARIO2_HPA:-deploy/03-evaluation/hpa-scenario2-catalogue.yaml}"
SCENARIO3_HPA="${SCENARIO3_HPA:-deploy/03-evaluation/hpa-scenario3-frontend.yaml}"

SCENARIO1_MIX="${SCENARIO1_MIX:-deploy/03-evaluation/workloads/sockshop-front-end-localcpu.yaml}"
SCENARIO2_MIX="${SCENARIO2_MIX:-deploy/03-evaluation/workloads/sockshop-catalogue-dependency.yaml}"
SCENARIO3_MIX="${SCENARIO3_MIX:-deploy/03-evaluation/workloads/sockshop-front-end-recovery.yaml}"
SOCKSHOP_GUARDRAILS="${SOCKSHOP_GUARDRAILS:-deploy/03-evaluation/sockshop-guardrails.yaml}"
FRONTEND_STABILITY_PATCH="${FRONTEND_STABILITY_PATCH:-deploy/03-evaluation/front-end-stability-patch.yaml}"

SCENARIO2_THROTTLE="${SCENARIO2_THROTTLE:-deploy/03-evaluation/scenario2-catalogue-db-throttle.yaml}"
SCENARIO2_DEFAULT="${SCENARIO2_DEFAULT:-deploy/03-evaluation/scenario2-catalogue-db-default.yaml}"

scenario_duration() {
  case "$1" in
    scenario1) echo 195 ;;
    scenario2) echo 130 ;;
    scenario3) echo 210 ;;
    *) echo "unknown scenario $1" >&2; exit 1 ;;
  esac
}

scenario_target() {
  case "$1" in
    scenario1|scenario3) echo "front-end" ;;
    scenario2) echo "catalogue" ;;
    *) echo "unknown scenario $1" >&2; exit 1 ;;
  esac
}

scenario_mix() {
  case "$1" in
    scenario1) echo "$SCENARIO1_MIX" ;;
    scenario2) echo "$SCENARIO2_MIX" ;;
    scenario3) echo "$SCENARIO3_MIX" ;;
    *) echo "unknown scenario $1" >&2; exit 1 ;;
  esac
}

scenario_slo() {
  case "$1" in
    scenario1) echo "$SCENARIO1_SLO" ;;
    scenario2) echo "$SCENARIO2_SLO" ;;
    scenario3) echo "$SCENARIO3_SLO" ;;
    *) echo "unknown scenario $1" >&2; exit 1 ;;
  esac
}

scenario_hpa() {
  case "$1" in
    scenario1) echo "$SCENARIO1_HPA" ;;
    scenario2) echo "$SCENARIO2_HPA" ;;
    scenario3) echo "$SCENARIO3_HPA" ;;
    *) echo "unknown scenario $1" >&2; exit 1 ;;
  esac
}

frontend_url() {
  local node_ip node_port
  node_ip="$($KUBECTL get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')"
  node_port="$($KUBECTL get svc front-end -n "$APP_NS" -o jsonpath='{.spec.ports[0].nodePort}')"
  echo "http://${node_ip}:${node_port}"
}

ensure_controller_image() {
  if ! docker image inspect loshans/controller:v1 >/dev/null 2>&1; then
    DOCKER_BUILDKIT=0 docker build -t loshans/controller:v1 src/controller >/dev/null 2>&1
  fi
  docker save loshans/controller:v1 | sudo k3s ctr images import - >/dev/null 2>&1
}

aggregator_url() {
  local node_ip node_port
  node_ip="$($KUBECTL get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')"
  node_port="$($KUBECTL get svc aggregator -n "$CONTROL_NS" -o jsonpath='{.spec.ports[0].nodePort}')"
  echo "http://${node_ip}:${node_port}"
}

cleanup_demo_workloads() {
  $KUBECTL delete -n "$APP_NS" -f deploy/02-demo-apps/workloads.yaml --ignore-not-found >/dev/null 2>&1 || true
}

reset_baseline() {
  cleanup_demo_workloads
  $KUBECTL apply -f "$SOCKSHOP_GUARDRAILS" >/dev/null
  if $KUBECTL get deploy front-end -n "$APP_NS" >/dev/null 2>&1; then
    $KUBECTL patch deploy/front-end -n "$APP_NS" --type strategic --patch-file "$FRONTEND_STABILITY_PATCH" >/dev/null
  fi
  $KUBECTL delete hpa --all -n "$APP_NS" --ignore-not-found >/dev/null 2>&1 || true
  $KUBECTL delete hpa front-end-hpa catalogue-hpa -n default --ignore-not-found >/dev/null 2>&1 || true
  $KUBECTL delete serviceslo --all -n "$APP_NS" --ignore-not-found >/dev/null 2>&1 || true
  for svc in front-end catalogue carts orders catalogue-db carts-db orders-db user user-db payment queue-master rabbitmq session-db shipping; do
    if $KUBECTL get deploy "$svc" -n "$APP_NS" >/dev/null 2>&1; then
      $KUBECTL scale deploy/"$svc" -n "$APP_NS" --replicas=1 >/dev/null
    fi
  done
  $KUBECTL rollout status deploy/front-end -n "$APP_NS" --timeout=240s >/dev/null
  $KUBECTL rollout status deploy/catalogue -n "$APP_NS" --timeout=240s >/dev/null
  $KUBECTL rollout status deploy/catalogue-db -n "$APP_NS" --timeout=240s >/dev/null
  $KUBECTL exec -n "$CONTROL_NS" deploy/redis -- redis-cli FLUSHALL >/dev/null 2>&1 || true
  $KUBECTL rollout restart deploy/aggregator -n "$CONTROL_NS" >/dev/null
  $KUBECTL rollout restart daemonset/bpf-agent -n "$CONTROL_NS" >/dev/null
  $KUBECTL rollout status deploy/aggregator -n "$CONTROL_NS" --timeout=240s >/dev/null
  $KUBECTL rollout status daemonset/bpf-agent -n "$CONTROL_NS" --timeout=240s >/dev/null
  sleep 8
}

set_scaler_mode() {
  local scaler="$1"
  if [ "$scaler" = "hpa" ]; then
    $KUBECTL scale deploy/custom-autoscaler -n "$CONTROL_NS" --replicas=0 >/dev/null
  else
    ensure_controller_image
    $KUBECTL scale deploy/custom-autoscaler -n "$CONTROL_NS" --replicas=1 >/dev/null
    $KUBECTL rollout status deploy/custom-autoscaler -n "$CONTROL_NS" --timeout=240s >/dev/null
  fi
}

apply_scenario_overrides() {
  local scenario="$1"
  if [ "$scenario" = "scenario2" ]; then
    $KUBECTL patch deploy/catalogue-db -n "$APP_NS" --type strategic --patch-file "$SCENARIO2_THROTTLE" >/dev/null
    $KUBECTL rollout status deploy/catalogue-db -n "$APP_NS" --timeout=240s >/dev/null
    sleep 5
  fi
}

revert_scenario_overrides() {
  local scenario="$1"
  if [ "$scenario" = "scenario2" ]; then
    $KUBECTL patch deploy/catalogue-db -n "$APP_NS" --type strategic --patch-file "$SCENARIO2_DEFAULT" >/dev/null
    $KUBECTL rollout status deploy/catalogue-db -n "$APP_NS" --timeout=240s >/dev/null
    sleep 5
  fi
}

run_case() {
  local scenario="$1"
  local scaler="$2"
  local target mix slo_file hpa_file duration fe_url agg_url result_dir csv

  target="$(scenario_target "$scenario")"
  mix="$(scenario_mix "$scenario")"
  slo_file="$(scenario_slo "$scenario")"
  hpa_file="$(scenario_hpa "$scenario")"
  duration="$(scenario_duration "$scenario")"

  result_dir="${RESULT_ROOT}/${scenario}"
  mkdir -p "$result_dir"
  csv="${result_dir}/results_${scenario}_${scaler}.csv"

  reset_baseline
  apply_scenario_overrides "$scenario"
  $KUBECTL apply -f "$slo_file" >/dev/null
  set_scaler_mode "$scaler"
  if [ "$scaler" = "hpa" ]; then
    $KUBECTL apply -n "$APP_NS" -f "$hpa_file" >/dev/null
  fi

  fe_url="$(frontend_url)"
  agg_url="$(aggregator_url)"

  $PYTHON src/load-generator/eval_harness.py \
    --url "$fe_url" \
    --deployment "$target" \
    --namespace "$APP_NS" \
    --profile sockshop \
    --mix-file "$mix" \
    --duration "$duration" \
    --warmup-seconds 0 \
    --sample-interval 2 \
    --aggregator-url "$agg_url" \
    --control-target "$target" \
    --csv "$csv"

  revert_scenario_overrides "$scenario"
}

main() {
  local only="${1:-}"
  local scenarios=(scenario1 scenario2 scenario3)
  if [ -n "$only" ]; then
    scenarios=("$only")
  fi
  for scenario in "${scenarios[@]}"; do
    run_case "$scenario" hpa
    run_case "$scenario" thrivescale
  done
  reset_baseline
  $KUBECTL scale deploy/custom-autoscaler -n "$CONTROL_NS" --replicas=1 >/dev/null
  $KUBECTL rollout status deploy/custom-autoscaler -n "$CONTROL_NS" --timeout=240s >/dev/null
}

main "$@"
