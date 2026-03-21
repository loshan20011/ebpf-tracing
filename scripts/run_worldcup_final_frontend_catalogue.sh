#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export MIX_FILE="${MIX_FILE:-deploy/03-evaluation/workloads/worldcup98-day75-browse.yaml}"
export HPA_FILE="${HPA_FILE:-deploy/03-evaluation/hpa-sockshop-paperlike.yaml}"
export SLO_FILE="${SLO_FILE:-deploy/03-evaluation/sockshop-slos.calibrated.yaml}"
export SERVICES="${SERVICES:-front-end catalogue catalogue-db}"
export SCALER_SERVICES="${SCALER_SERVICES:-front-end catalogue}"
export CONTROL_TARGET="${CONTROL_TARGET:-front-end}"
export CLIENT_SLO_MS="${CLIENT_SLO_MS:-41}"
export RESULT_DIR="${RESULT_DIR:-results/worldcup-final-frontend-catalogue}"
export ANALYSIS_DIR="${ANALYSIS_DIR:-results/analysis/worldcup-final-frontend-catalogue}"

echo "[info] final front-end + catalogue evaluation"
echo "[info] mix_file=$MIX_FILE"
echo "[info] hpa_file=$HPA_FILE"
echo "[info] slo_file=$SLO_FILE"
echo "[info] services=$SERVICES"
echo "[info] scaler_services=$SCALER_SERVICES"
echo "[note] This is the thesis final track: WorldCup98 peak 10-minute browse replay over the validated front-end -> catalogue path."
echo "[note] This is paper-inspired rather than identical to STaleX, because the evaluated service chain is front-end + catalogue instead of /login."

bash scripts/run_worldcup_paperlike.sh
