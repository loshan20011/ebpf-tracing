#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export SLO_FILE="${SLO_FILE:-deploy/03-evaluation/sockshop-slos.stalex-paper.yaml}"
export HPA_FILE="${HPA_FILE:-deploy/03-evaluation/hpa-sockshop-stalex-diff-50-50-25.yaml}"
export MIX_FILE="${MIX_FILE:-deploy/03-evaluation/workloads/worldcup98-day75-peak.yaml}"
export CONTROL_TARGET="${CONTROL_TARGET:-front-end}"
export CLIENT_SLO_MS="${CLIENT_SLO_MS:-200}"
export SERVICES="${SERVICES:-front-end user carts}"
export SCALER_SERVICES="${SCALER_SERVICES:-front-end user carts}"
export RESULT_DIR="${RESULT_DIR:-results/worldcup-stalex-paper}"
export ANALYSIS_DIR="${ANALYSIS_DIR:-results/analysis/worldcup-stalex-paper}"

echo "[info] paper-aligned STaleX evaluation"
echo "[info] HPA thresholds file: $HPA_FILE"
echo "[info] SLO file: $SLO_FILE"
echo "[info] Workload file: $MIX_FILE"
echo "[info] Client SLO ms: $CLIENT_SLO_MS"
echo "[note] Exact deviation from the paper: this runner reuses the WorldCup98-derived time series in this repo, but not the paper's original Locust login user-journey implementation."
echo "[note] Reference details are documented in deploy/03-evaluation/PAPER_ALIGNMENT.md"

bash scripts/run_worldcup_paperlike.sh
