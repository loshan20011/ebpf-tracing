#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export MIX_FILE="${MIX_FILE:-deploy/03-evaluation/workloads/worldcup98-day75-cart-checkout.yaml}"
export HPA_FILE="${HPA_FILE:-deploy/03-evaluation/hpa-sockshop-cart-checkout.yaml}"
export SLO_FILE="${SLO_FILE:-deploy/03-evaluation/sockshop-slos.cart-checkout.yaml}"
export SERVICES="${SERVICES:-front-end carts orders carts-db orders-db rabbitmq payment shipping}"
export SCALER_SERVICES="${SCALER_SERVICES:-front-end carts orders}"
export CONTROL_TARGET="${CONTROL_TARGET:-orders}"
export CLIENT_SLO_MS="${CLIENT_SLO_MS:-120}"
export RESULT_DIR="${RESULT_DIR:-results/worldcup-final-cart-checkout}"
export ANALYSIS_DIR="${ANALYSIS_DIR:-results/analysis/worldcup-final-cart-checkout}"
export RUNS="${RUNS:-1}"

echo "[info] final front-end + carts + orders evaluation"
echo "[info] mix_file=$MIX_FILE"
echo "[info] hpa_file=$HPA_FILE"
echo "[info] slo_file=$SLO_FILE"
echo "[info] services=$SERVICES"
echo "[info] scaler_services=$SCALER_SERVICES"
echo "[note] This is the transactional thesis track: WorldCup98 peak replay over front-end -> carts -> orders style pages."
echo "[note] It is a thesis extension beyond the exact paper path and is meant to complement the browse and user-path scenarios."

bash scripts/run_worldcup_paperlike.sh
