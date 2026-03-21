#!/usr/bin/env bash
set -euo pipefail

APP_NS="${APP_NS:-sock-shop}"
CONTROL_NS="${CONTROL_NS:-thrive-scale}"
FREEZE_ROOT="${FREEZE_ROOT:-results/freeze}"
FREEZE_LABEL="${FREEZE_LABEL:-phase4_baseline}"
RUNNER_SCRIPT="${RUNNER_SCRIPT:-scripts/run_case_validation.sh}"
CASE4_MIX="${CASE4_MIX:-/tmp/case-artifacts/case4-mix.yaml}"
CASE1_MIX="${CASE1_MIX:-deploy/03-evaluation/workloads/sockshop-catalogue-proof.yaml}"
CASE2_MIX="${CASE2_MIX:-deploy/03-evaluation/workloads/sockshop-ew-mix.yaml}"
CASE_ARTIFACTS_DIR="${CASE_ARTIFACTS_DIR:-/tmp/case-artifacts}"
PASS_TS="${PASS_TS:-}"
PROFILE_FILE="${PROFILE_FILE:-}"
CONTROLLER_FILE="${CONTROLLER_FILE:-src/controller/controller.py}"

TS="$(date +%Y%m%d_%H%M%S)"
OUT_DIR="${FREEZE_ROOT}/${FREEZE_LABEL}_${TS}"
mkdir -p "$OUT_DIR" "$OUT_DIR/artifacts"

if [[ ! -f "$RUNNER_SCRIPT" ]]; then
  echo "[error] runner script not found: $RUNNER_SCRIPT"
  exit 1
fi

echo "[info] writing freeze bundle to $OUT_DIR"

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git rev-parse HEAD > "$OUT_DIR/commit.txt"
  git status --short > "$OUT_DIR/git_status_short.txt" || true
  git diff --stat > "$OUT_DIR/git_diffstat.txt" || true
else
  echo "not_a_git_checkout" > "$OUT_DIR/commit.txt"
  echo "[warn] current directory is not a git checkout" | tee -a "$OUT_DIR/warnings.txt"
fi

kubectl get deploy -A -o custom-columns='namespace:.metadata.namespace,deployment:.metadata.name,image:.spec.template.spec.containers[*].image' \
  > "$OUT_DIR/deploy_images.txt"

cat > "$OUT_DIR/metadata.env" <<META
APP_NS=$APP_NS
CONTROL_NS=$CONTROL_NS
RUNNER_SCRIPT=$RUNNER_SCRIPT
CASE1_MIX=$CASE1_MIX
CASE2_MIX=$CASE2_MIX
CASE4_MIX=$CASE4_MIX
CASE_ARTIFACTS_DIR=$CASE_ARTIFACTS_DIR
PASS_TS=$PASS_TS
FREEZE_CREATED_AT=$(date -u +%FT%TZ)
META

cp "$RUNNER_SCRIPT" "$OUT_DIR/run_case_validation.sh"
if [[ -f "$CONTROLLER_FILE" ]]; then
  cp "$CONTROLLER_FILE" "$OUT_DIR/controller.py"
fi
for f in "$CASE1_MIX" "$CASE2_MIX" "$CASE4_MIX"; do
  if [[ -f "$f" ]]; then
    cp "$f" "$OUT_DIR/"
  else
    echo "[warn] mix missing: $f" | tee -a "$OUT_DIR/warnings.txt"
  fi
done
if [[ -n "$PROFILE_FILE" ]]; then
  if [[ -f "$PROFILE_FILE" ]]; then
    cp "$PROFILE_FILE" "$OUT_DIR/"
  else
    echo "[warn] profile file missing: $PROFILE_FILE" | tee -a "$OUT_DIR/warnings.txt"
  fi
fi

if [[ -n "$PASS_TS" ]]; then
  shopt -s nullglob
  matches=("$CASE_ARTIFACTS_DIR"/*"_${PASS_TS}"*)
  shopt -u nullglob
  if (( ${#matches[@]} > 0 )); then
    cp -a "${matches[@]}" "$OUT_DIR/artifacts/"
  else
    echo "[warn] no artifacts found for PASS_TS=$PASS_TS in $CASE_ARTIFACTS_DIR" | tee -a "$OUT_DIR/warnings.txt"
  fi
else
  if [[ -d "$CASE_ARTIFACTS_DIR" ]]; then
    cp -a "$CASE_ARTIFACTS_DIR"/. "$OUT_DIR/artifacts/"
  else
    echo "[warn] artifacts directory missing: $CASE_ARTIFACTS_DIR" | tee -a "$OUT_DIR/warnings.txt"
  fi
fi

if command -v sha256sum >/dev/null 2>&1; then
  (cd "$OUT_DIR" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)
fi

echo "[done] freeze bundle created: $OUT_DIR"
