#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${NAMESPACE:-sock-shop}"
REPO_URL="${REPO_URL:-https://github.com/ocp-power-demos/sock-shop-demo.git}"
REPO_DIR="${REPO_DIR:-/tmp/sock-shop-demo}"
KUBECTL="${KUBECTL:-kubectl}"
MONGO_USER="${MONGO_USER:-root}"
MONGO_PASSWORD="${MONGO_PASSWORD:-admin}"
SESSION_DB_IMAGE="${SESSION_DB_IMAGE:-redis:7-alpine}"
FRONTEND_SERVICE_TYPE="${FRONTEND_SERVICE_TYPE:-LoadBalancer}"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

clone_repo() {
  if [[ -d "$REPO_DIR/.git" ]]; then
    git -C "$REPO_DIR" fetch --depth 1 origin main >/dev/null 2>&1 || true
    git -C "$REPO_DIR" reset --hard origin/main >/dev/null 2>&1 || true
  else
    rm -rf "$REPO_DIR"
    git clone --depth 1 "$REPO_URL" "$REPO_DIR"
  fi
}

install_base_resources() {
  "$KUBECTL" delete namespace "$NAMESPACE" --ignore-not-found=true --wait=true || true
  "$KUBECTL" create namespace "$NAMESPACE"

  "$KUBECTL" -n "$NAMESPACE" create secret generic mongodb-creds \
    --from-literal=username="$MONGO_USER" \
    --from-literal=password="$MONGO_PASSWORD"

  for file in "$REPO_DIR"/manifests/overlays/single/*.yaml; do
    base="$(basename "$file")"
    case "$base" in
      env.secret|kustomization.yaml|29-route-front-end.yaml)
        continue
        ;;
    esac
    "$KUBECTL" apply -n "$NAMESPACE" -f "$file"
  done
}

normalize_storage() {
  "$KUBECTL" delete pvc carts-db-temp-pvc orders-db-temp-pvc user-db-temp-pvc -n "$NAMESPACE" --ignore-not-found=true || true

  "$KUBECTL" patch deploy carts-db -n "$NAMESPACE" --type=json -p='[
    {"op":"replace","path":"/spec/template/spec/volumes/0","value":{"name":"carts-db-temp-vol","emptyDir":{}}},
    {"op":"replace","path":"/spec/template/spec/volumes/1","value":{"name":"carts-db-vol","emptyDir":{"medium":"Memory"}}}
  ]' >/dev/null

  "$KUBECTL" patch deploy orders-db -n "$NAMESPACE" --type=json -p='[
    {"op":"replace","path":"/spec/template/spec/volumes/0","value":{"name":"orders-db-vol","emptyDir":{"medium":"Memory"}}},
    {"op":"replace","path":"/spec/template/spec/volumes/1","value":{"name":"orders-db-temp-vol","emptyDir":{}}}
  ]' >/dev/null

  "$KUBECTL" patch deploy user-db -n "$NAMESPACE" --type=json -p='[
    {"op":"replace","path":"/spec/template/spec/volumes/0","value":{"name":"users-db-vol","emptyDir":{"medium":"Memory"}}},
    {"op":"replace","path":"/spec/template/spec/volumes/1","value":{"name":"users-db-temp-vol","emptyDir":{}}}
  ]' >/dev/null
}

patch_deployments() {
  local deploys=(
    carts carts-db catalogue catalogue-db front-end orders orders-db
    payment queue-master rabbitmq session-db shipping user user-db
  )

  for d in "${deploys[@]}"; do
    "$KUBECTL" patch deploy "$d" -n "$NAMESPACE" --type=json \
      -p='[{"op":"remove","path":"/spec/template/spec/nodeSelector"}]' >/dev/null 2>&1 || true
  done

  for d in carts catalogue front-end orders payment queue-master shipping user rabbitmq; do
    "$KUBECTL" patch deploy "$d" -n "$NAMESPACE" --type=json \
      -p='[{"op":"remove","path":"/spec/template/spec/containers/0/securityContext/runAsNonRoot"}]' >/dev/null 2>&1 || true
    "$KUBECTL" patch deploy "$d" -n "$NAMESPACE" --type=json \
      -p='[{"op":"remove","path":"/spec/template/spec/containers/1/securityContext/runAsNonRoot"}]' >/dev/null 2>&1 || true
  done

  "$KUBECTL" patch deploy/session-db -n "$NAMESPACE" --type=merge -p "$(cat <<EOF
{
  "spec": {
    "template": {
      "spec": {
        "containers": [
          {
            "name": "session-db",
            "image": "${SESSION_DB_IMAGE}"
          }
        ]
      }
    }
  }
}
EOF
)" >/dev/null
  "$KUBECTL" patch deploy/session-db -n "$NAMESPACE" --type=json \
    -p='[{"op":"replace","path":"/spec/template/spec/containers/0/securityContext","value":{"runAsUser":0}}]' >/dev/null
  "$KUBECTL" patch deploy/catalogue-db -n "$NAMESPACE" --type=json \
    -p='[{"op":"replace","path":"/spec/template/spec/containers/0/securityContext","value":{"runAsUser":0}}]' >/dev/null
}

patch_benchmark_readiness() {
  "$KUBECTL" patch deploy/user -n "$NAMESPACE" --type=merge -p "$(cat <<'EOF'
{
  "spec": {
    "template": {
      "spec": {
        "containers": [
          {
            "name": "user",
            "readinessProbe": {
              "httpGet": {
                "path": "/health",
                "port": 8080
              },
              "initialDelaySeconds": 30,
              "periodSeconds": 5,
              "timeoutSeconds": 2,
              "failureThreshold": 6
            },
            "livenessProbe": {
              "httpGet": {
                "path": "/health",
                "port": 8080
              },
              "initialDelaySeconds": 60,
              "periodSeconds": 15,
              "timeoutSeconds": 2,
              "failureThreshold": 3
            }
          }
        ]
      }
    }
  }
}
EOF
)" >/dev/null

  "$KUBECTL" patch deploy/carts -n "$NAMESPACE" --type=merge -p "$(cat <<'EOF'
{
  "spec": {
    "minReadySeconds": 15,
    "strategy": {
      "type": "RollingUpdate",
      "rollingUpdate": {
        "maxSurge": 1,
        "maxUnavailable": 0
      }
    },
    "template": {
      "spec": {
        "containers": [
          {
            "name": "carts",
            "args": [
              "-cp",
              "/opt/app.jar",
              "-Xms32m",
              "-Xmx96m",
              "-XX:+UseG1GC",
              "-Djava.security.egd=file:/dev/urandom",
              "-Dspring.zipkin.enabled=false",
              "-Dloader.path=/opt/lib",
              "org.springframework.boot.loader.PropertiesLauncher",
              "--port=8080"
            ],
            "resources": {
              "requests": {
                "cpu": "300m",
                "memory": "512Mi"
              },
              "limits": {
                "cpu": "600m",
                "memory": "1Gi"
              }
            },
            "startupProbe": {
              "httpGet": {
                "path": "/health",
                "port": 8080
              },
              "initialDelaySeconds": 20,
              "periodSeconds": 5,
              "timeoutSeconds": 5,
              "failureThreshold": 48
            },
            "readinessProbe": {
              "httpGet": {
                "path": "/health",
                "port": 8080
              },
              "initialDelaySeconds": 0,
              "periodSeconds": 10,
              "timeoutSeconds": 5,
              "failureThreshold": 6
            },
            "livenessProbe": {
              "httpGet": {
                "path": "/health",
                "port": 8080
              },
              "initialDelaySeconds": 0,
              "periodSeconds": 20,
              "timeoutSeconds": 5,
              "failureThreshold": 6
            }
          }
        ]
      }
    }
  }
}
EOF
)" >/dev/null
}

expose_frontend() {
  "$KUBECTL" patch svc front-end -n "$NAMESPACE" --type=merge -p "$(cat <<EOF
{
  "spec": {
    "type": "${FRONTEND_SERVICE_TYPE}",
    "ports": [
      {
        "port": 80,
        "targetPort": 8079,
        "protocol": "TCP"
      }
    ]
  }
}
EOF
)" >/dev/null
}

restart_and_wait() {
  local deploys=(
    carts carts-db catalogue catalogue-db front-end orders orders-db
    payment queue-master rabbitmq session-db shipping user user-db
  )

  for d in "${deploys[@]}"; do
    "$KUBECTL" scale deploy "$d" -n "$NAMESPACE" --replicas=1 >/dev/null 2>&1 || true
  done

  for d in "${deploys[@]}"; do
    "$KUBECTL" rollout restart deploy "$d" -n "$NAMESPACE" >/dev/null 2>&1 || true
  done

  for d in "${deploys[@]}"; do
    "$KUBECTL" rollout status deploy "$d" -n "$NAMESPACE" --timeout=300s
  done
}

show_summary() {
  echo
  echo "[ok] Sock Shop deployed in namespace: $NAMESPACE"
  echo "[ok] Temp database storage mapped to emptyDir volumes for EKS compatibility"
  "$KUBECTL" get deploy -n "$NAMESPACE"
  echo "---"
  "$KUBECTL" get pods -n "$NAMESPACE"
  echo "---"
  "$KUBECTL" get svc -n "$NAMESPACE"
}

main() {
  require_cmd git
  require_cmd "$KUBECTL"

  clone_repo
  install_base_resources
  normalize_storage
  patch_deployments
  patch_benchmark_readiness
  expose_frontend
  restart_and_wait
  show_summary
}

main "$@"
