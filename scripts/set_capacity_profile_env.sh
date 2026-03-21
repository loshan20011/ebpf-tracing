#!/usr/bin/env bash
set -euo pipefail

CONTROL_NS="${CONTROL_NS:-thrive-scale}"
KUBECTL="${KUBECTL:-kubectl}"
PROFILE_FILE="${1:-results/capacity_profiles/capacity_profiles.json}"

if [[ ! -f "$PROFILE_FILE" ]]; then
  echo "[error] profile file not found: $PROFILE_FILE"
  exit 1
fi

profile_points="$(python3 - <<'PY' "$PROFILE_FILE"
import json,sys
obj=json.load(open(sys.argv[1],'r',encoding='utf-8'))
services=obj.get('services', obj if isinstance(obj, dict) else {})
total=0
if isinstance(services, dict):
    for _, rows in services.items():
        if isinstance(rows, list):
            total += len(rows)
print(total)
PY
)"
if [[ "${profile_points:-0}" -le 0 ]]; then
  echo "[error] profile has zero capacity points; refusing to enable CAPACITY_PROFILE_ENABLED=true"
  echo "        regenerate profile first, then re-run this script"
  exit 2
fi

PROFILE_JSON_MINIFIED="$(python3 - <<'PY' "$PROFILE_FILE"
import json,sys
p=sys.argv[1]
obj=json.load(open(p,'r',encoding='utf-8'))
print(json.dumps(obj.get('services', obj), separators=(',',':')))
PY
)"

$KUBECTL -n "$CONTROL_NS" set env deploy/custom-autoscaler \
  CAPACITY_PROFILE_ENABLED=true \
  CAPACITY_PROFILE_JSON="$PROFILE_JSON_MINIFIED" \
  CAPACITY_PROFILE_SERVICES="${CAPACITY_PROFILE_SERVICES:-}" \
  CAPACITY_PROFILE_HEADROOM="${CAPACITY_PROFILE_HEADROOM:-1.15}" \
  CAPACITY_PROFILE_RELOAD_S="${CAPACITY_PROFILE_RELOAD_S:-30}" \
  CAPACITY_PROFILE_PANIC_UPLIFT_RATIO="${CAPACITY_PROFILE_PANIC_UPLIFT_RATIO:-0.50}" \
  CAPACITY_PROFILE_PANIC_MIN_STEP="${CAPACITY_PROFILE_PANIC_MIN_STEP:-2}"

$KUBECTL rollout restart -n "$CONTROL_NS" deploy/custom-autoscaler
$KUBECTL rollout status -n "$CONTROL_NS" deploy/custom-autoscaler --timeout=240s

echo "[done] capacity profile env applied from $PROFILE_FILE"
echo "       CAPACITY_PROFILE_SERVICES=${CAPACITY_PROFILE_SERVICES:-<all>}"
