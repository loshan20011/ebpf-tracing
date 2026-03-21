#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export MIX_FILE="${MIX_FILE:-deploy/03-evaluation/workloads/worldcup98-day75-user-loginlike.yaml}"
export HPA_FILE="${HPA_FILE:-deploy/03-evaluation/hpa-sockshop-user-paperlike.yaml}"
export SLO_FILE="${SLO_FILE:-deploy/03-evaluation/sockshop-slos.user-paperlike.yaml}"
export SERVICES="${SERVICES:-front-end user user-db}"
export SCALER_SERVICES="${SCALER_SERVICES:-front-end user}"
export CONTROL_TARGET="${CONTROL_TARGET:-user}"
export CLIENT_SLO_MS="${CLIENT_SLO_MS:-200}"
export RESULT_DIR="${RESULT_DIR:-results/worldcup-final-user-loginlike}"
export ANALYSIS_DIR="${ANALYSIS_DIR:-results/analysis/worldcup-final-user-loginlike}"
export RUNS="${RUNS:-1}"

echo "[info] final front-end + user evaluation"
echo "[info] mix_file=$MIX_FILE"
echo "[info] hpa_file=$HPA_FILE"
echo "[info] slo_file=$SLO_FILE"
echo "[info] services=$SERVICES"
echo "[info] scaler_services=$SCALER_SERVICES"
echo "[note] This is the paper-closer thesis track: WorldCup98 peak replay over a login-like front-end -> user path."
echo "[note] It remains paper-inspired because the traffic driver is the repo's WorldCup harness rather than the paper's original Locust script."

bash scripts/run_worldcup_paperlike.sh
