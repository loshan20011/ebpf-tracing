#!/usr/bin/env bash
set -euo pipefail

kubectl patch svc traefik -n kube-system --type=merge -p '{"spec":{"type":"ClusterIP"}}'
kubectl get svc traefik -n kube-system -o wide
