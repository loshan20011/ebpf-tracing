#!/usr/bin/env bash
set -euo pipefail

APP_NS="${APP_NS:-sock-shop}"
CONTROL_NS="${CONTROL_NS:-thrive-scale}"
KUBECTL="${KUBECTL:-kubectl}"

"$KUBECTL" delete namespace "$APP_NS" --ignore-not-found=true --wait=true || true
"$KUBECTL" delete namespace "$CONTROL_NS" --ignore-not-found=true --wait=true || true
"$KUBECTL" delete crd serviceslos.autoscaling.fyp.io --ignore-not-found=true || true
"$KUBECTL" delete clusterrole aggregator-reader autoscaler-role --ignore-not-found=true || true
"$KUBECTL" delete clusterrolebinding aggregator-binding autoscaler-binding --ignore-not-found=true || true
"$KUBECTL" get ns
