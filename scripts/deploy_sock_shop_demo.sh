#!/usr/bin/env bash
set -euo pipefail

# Keep Sock Shop pinned to the ocp-power-demos fork used for this project.
SOCKSHOP_REPO="https://github.com/ocp-power-demos/sock-shop-demo.git"
SOCKSHOP_DIR="${SOCKSHOP_DIR:-$HOME/sock-shop-demo}"
APP_NS="${APP_NS:-sock-shop}"
SOCKSHOP_COMPONENT_SET="${SOCKSHOP_COMPONENT_SET:-all}"
KUBECTL="${KUBECTL:-kubectl}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
GUARDRAILS_FILE="${SOCKSHOP_GUARDRAILS_FILE:-$ROOT_DIR/deploy/03-evaluation/sockshop-guardrails.yaml}"

require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "Missing required command: $1" >&2
        exit 1
    fi
}

prepare_repo() {
    if [ ! -d "$SOCKSHOP_DIR/.git" ]; then
        git clone "$SOCKSHOP_REPO" "$SOCKSHOP_DIR"
    else
        echo "Using existing Sock Shop clone at $SOCKSHOP_DIR"
    fi
}

patch_repo_for_k3s() {
    python3 - "$SOCKSHOP_DIR" "$APP_NS" <<'PY'
from pathlib import Path
import re
import sys

root = Path(sys.argv[1])
namespace = sys.argv[2]
base = root / "manifests" / "base"

(base / "env.secret").write_text("username=admin\npassword=password\n", encoding="utf-8")
(base / "00-sock-shop-ns.yaml").write_text(
    f"apiVersion: v1\nkind: Namespace\nmetadata:\n  name: {namespace}\n",
    encoding="utf-8",
)

kustomization = base / "kustomization.yaml"
kustom_text = kustomization.read_text(encoding="utf-8")
kustom_text = kustom_text.replace("  - 29-route-front-end.yaml\n", "")
kustom_text = kustom_text.replace("  secretGenerator:\n", "secretGenerator:\n")
kustomization.write_text(kustom_text, encoding="utf-8")

for manifest in base.glob("*.yaml"):
    text = manifest.read_text(encoding="utf-8")
    text = text.replace("namespace: sock-shop", f"namespace: {namespace}")
    text = re.sub(r"(?ms)^\s*nodeSelector:\n(?:\s+.+\n)+", "", text)
    text = re.sub(r"(?m)^\s*runAsNonRoot:\s*true\s*\n", "", text)
    text = text.replace("storageClassName: nfs-client", "storageClassName: local-path")
    text = re.sub(r"registry\.redhat\.io/rhel9/redis-7:[^\s]+", "redis:7-alpine", text)
    text = text.replace("quay.io/powercloud/sock-shop-carts:latest", "weaveworksdemos/carts:0.4.8")
    text = text.replace("quay.io/powercloud/sock-shop-catalogue:latest", "weaveworksdemos/catalogue:0.3.5")
    text = text.replace("quay.io/powercloud/sock-shop-catalogue-db:latest", "weaveworksdemos/catalogue-db:0.3.0")
    text = text.replace("quay.io/powercloud/sock-shop-front-end:latest", "weaveworksdemos/front-end:0.3.12")
    text = text.replace("quay.io/powercloud/sock-shop-orders:latest", "weaveworksdemos/orders:0.4.7")
    text = text.replace("quay.io/powercloud/sock-shop-payment:latest", "weaveworksdemos/payment:0.4.3")
    text = text.replace("quay.io/powercloud/sock-shop-queue-master:latest", "weaveworksdemos/queue-master:0.3.1")
    text = text.replace("quay.io/powercloud/rabbitmq:latest", "rabbitmq:3.6.8-management")
    text = text.replace("ghcr.io/kbudde/rabbitmq_exporter:1.0.0", "kbudde/rabbitmq-exporter")
    text = text.replace("quay.io/powercloud/sock-shop-shipping:latest", "weaveworksdemos/shipping:0.4.8")
    text = text.replace("quay.io/powercloud/sock-shop-user:latest", "weaveworksdemos/user:0.4.7")
    text = text.replace("quay.io/powercloud/sock-shop-user-db:latest", "weaveworksdemos/user-db:0.3.0")
    manifest.write_text(text, encoding="utf-8")

overrides = {
    "01-carts-dep.yaml": f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: carts
  labels:
    name: carts
  namespace: {namespace}
spec:
  replicas: 1
  selector:
    matchLabels:
      name: carts
  template:
    metadata:
      labels:
        name: carts
    spec:
      containers:
        - name: carts
          image: weaveworksdemos/carts:0.4.8
          imagePullPolicy: IfNotPresent
          env:
            - name: JAVA_OPTS
              value: -Xms64m -Xmx256m -XX:+UseG1GC -Djava.security.egd=file:/dev/urandom -Dspring.zipkin.enabled=false
          resources:
            limits:
              cpu: 500m
              memory: 1Gi
            requests:
              cpu: 100m
              memory: 300Mi
          ports:
            - containerPort: 80
          startupProbe:
            tcpSocket:
              port: 80
            failureThreshold: 120
            periodSeconds: 5
            timeoutSeconds: 1
          readinessProbe:
            tcpSocket:
              port: 80
            periodSeconds: 5
            failureThreshold: 6
            timeoutSeconds: 1
          livenessProbe:
            tcpSocket:
              port: 80
            periodSeconds: 10
            failureThreshold: 3
            timeoutSeconds: 1
          securityContext:
            readOnlyRootFilesystem: true
          volumeMounts:
            - mountPath: /tmp
              name: carts-vol
      volumes:
        - name: carts-vol
          emptyDir:
            medium: Memory
""",
    "03-carts-db-dep.yaml": f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: carts-db
  labels:
    name: carts-db
  namespace: {namespace}
spec:
  replicas: 1
  selector:
    matchLabels:
      name: carts-db
  template:
    metadata:
      labels:
        name: carts-db
    spec:
      containers:
        - name: carts-db
          image: quay.io/mongodb/mongodb:org-4.4-standalone-ubuntu2204
          imagePullPolicy: IfNotPresent
          ports:
            - name: mongo
              containerPort: 27017
          securityContext:
            readOnlyRootFilesystem: true
          volumeMounts:
            - mountPath: /tmp
              name: carts-db-temp-vol
            - mountPath: /data/db
              name: carts-db-vol
      volumes:
        - name: carts-db-temp-vol
          emptyDir: {{}}
        - name: carts-db-vol
          emptyDir: {{}}
""",
    "05-catalogue-dep.yaml": f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: catalogue
  labels:
    name: catalogue
  namespace: {namespace}
spec:
  replicas: 1
  selector:
    matchLabels:
      name: catalogue
  template:
    metadata:
      labels:
        name: catalogue
    spec:
      containers:
        - name: catalogue
          image: weaveworksdemos/catalogue:0.3.5
          imagePullPolicy: IfNotPresent
          command: ["/app"]
          args: ["-port=80"]
          resources:
            limits:
              cpu: 200m
              memory: 200Mi
            requests:
              cpu: 100m
              memory: 100Mi
          ports:
            - containerPort: 80
          securityContext:
            readOnlyRootFilesystem: true
          livenessProbe:
            httpGet:
              path: /health
              port: 80
            initialDelaySeconds: 300
            periodSeconds: 3
          readinessProbe:
            httpGet:
              path: /health
              port: 80
            initialDelaySeconds: 180
            periodSeconds: 3
""",
    "07-catalogue-db-dep.yaml": f"""---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: catalogue-db
  labels:
    name: catalogue-db
  namespace: {namespace}
spec:
  replicas: 1
  selector:
    matchLabels:
      name: catalogue-db
  template:
    metadata:
      labels:
        name: catalogue-db
    spec:
      containers:
        - name: catalogue-db
          image: weaveworksdemos/catalogue-db:0.3.0
          imagePullPolicy: IfNotPresent
          env:
            - name: MYSQL_ROOT_PASSWORD
              value: fake_password
            - name: MYSQL_DATABASE
              value: socksdb
          ports:
            - name: mysql
              containerPort: 3306
""",
    "09-front-end-dep.yaml": f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: front-end
  namespace: {namespace}
spec:
  replicas: 1
  selector:
    matchLabels:
      name: front-end
  template:
    metadata:
      labels:
        name: front-end
    spec:
      containers:
        - name: front-end
          image: weaveworksdemos/front-end:0.3.12
          imagePullPolicy: IfNotPresent
          resources:
            limits:
              cpu: 300m
              memory: 1000Mi
            requests:
              cpu: 100m
              memory: 300Mi
          ports:
            - containerPort: 8079
          env:
            - name: SESSION_REDIS
              value: "true"
          securityContext:
            readOnlyRootFilesystem: true
""",
    "11-orders-dep.yaml": f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: orders
  labels:
    name: orders
  namespace: {namespace}
spec:
  replicas: 1
  selector:
    matchLabels:
      name: orders
  template:
    metadata:
      labels:
        name: orders
    spec:
      containers:
        - name: orders
          image: weaveworksdemos/orders:0.4.7
          imagePullPolicy: IfNotPresent
          env:
            - name: JAVA_OPTS
              value: -Xms64m -Xmx256m -XX:+UseG1GC -Djava.security.egd=file:/dev/urandom -Dspring.zipkin.enabled=false
          resources:
            limits:
              cpu: 700m
              memory: 1Gi
            requests:
              cpu: 100m
              memory: 400Mi
          ports:
            - containerPort: 80
          startupProbe:
            tcpSocket:
              port: 80
            failureThreshold: 120
            periodSeconds: 5
            timeoutSeconds: 1
          readinessProbe:
            tcpSocket:
              port: 80
            periodSeconds: 5
            failureThreshold: 6
            timeoutSeconds: 1
          livenessProbe:
            tcpSocket:
              port: 80
            periodSeconds: 10
            failureThreshold: 3
            timeoutSeconds: 1
          securityContext:
            readOnlyRootFilesystem: true
          volumeMounts:
            - mountPath: /tmp
              name: tmp-volume
      volumes:
        - name: tmp-volume
          emptyDir:
            medium: Memory
""",
    "13-orders-db-dep.yaml": f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: orders-db
  labels:
    name: orders-db
  namespace: {namespace}
spec:
  replicas: 1
  selector:
    matchLabels:
      name: orders-db
  template:
    metadata:
      labels:
        name: orders-db
    spec:
      containers:
        - name: orders-db
          image: quay.io/mongodb/mongodb:org-4.4-standalone-ubuntu2204
          imagePullPolicy: IfNotPresent
          ports:
            - name: mongo
              containerPort: 27017
          securityContext:
            readOnlyRootFilesystem: true
          volumeMounts:
            - mountPath: /tmp
              name: orders-db-temp-vol
            - mountPath: /data/db
              name: orders-db-vol
      volumes:
        - name: orders-db-temp-vol
          emptyDir: {{}}
        - name: orders-db-vol
          emptyDir: {{}}
""",
    "21-session-db-dep.yaml": f"""---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: session-db
  labels:
    name: session-db
  namespace: {namespace}
spec:
  replicas: 1
  selector:
    matchLabels:
      name: session-db
  template:
    metadata:
      labels:
        name: session-db
    spec:
      containers:
        - name: session-db
          image: redis:alpine
          imagePullPolicy: IfNotPresent
          ports:
            - name: redis
              containerPort: 6379
          securityContext:
            capabilities:
              drop: ["all"]
              add: ["CHOWN", "SETGID", "SETUID"]
            readOnlyRootFilesystem: true
          volumeMounts:
            - mountPath: /data
              name: sesion-db-vol
      volumes:
        - name: sesion-db-vol
          emptyDir: {{}}
""",
    "15-payment-dep.yaml": f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: payment
  labels:
    name: payment
  namespace: {namespace}
spec:
  replicas: 1
  selector:
    matchLabels:
      name: payment
  template:
    metadata:
      labels:
        name: payment
    spec:
      containers:
        - name: payment
          image: weaveworksdemos/payment:0.4.3
          imagePullPolicy: IfNotPresent
          resources:
            limits:
              cpu: 200m
              memory: 200Mi
            requests:
              cpu: 99m
              memory: 100Mi
          ports:
            - containerPort: 80
          livenessProbe:
            httpGet:
              path: /health
              port: 80
            initialDelaySeconds: 60
            periodSeconds: 5
          readinessProbe:
            httpGet:
              path: /health
              port: 80
            initialDelaySeconds: 60
            periodSeconds: 5
          securityContext:
            capabilities:
              add: ["NET_BIND_SERVICE"]
              drop: ["all"]
            privileged: false
""",
    "17-queue-master-dep.yaml": f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: queue-master
  labels:
    name: queue-master
  namespace: {namespace}
spec:
  replicas: 1
  selector:
    matchLabels:
      name: queue-master
  template:
    metadata:
      labels:
        name: queue-master
    spec:
      containers:
        - name: queue-master
          image: weaveworksdemos/queue-master:0.3.1
          imagePullPolicy: IfNotPresent
          env:
            - name: JAVA_OPTS
              value: -Xms64m -Xmx128m -XX:+UseG1GC -Djava.security.egd=file:/dev/urandom -Dspring.zipkin.enabled=false
          resources:
            limits:
              cpu: 300m
              memory: 1Gi
            requests:
              cpu: 100m
              memory: 512Mi
          ports:
            - containerPort: 80
""",
    "19-rabbitmq-dep.yaml": f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: rabbitmq
  labels:
    name: rabbitmq
  namespace: {namespace}
spec:
  replicas: 1
  selector:
    matchLabels:
      name: rabbitmq
  template:
    metadata:
      labels:
        name: rabbitmq
      annotations:
        prometheus.io/scrape: "false"
    spec:
      containers:
        - name: rabbitmq
          image: rabbitmq:3.6.8-management
          imagePullPolicy: IfNotPresent
          ports:
            - name: management
              containerPort: 15672
            - name: rabbitmq
              containerPort: 5672
          securityContext:
            readOnlyRootFilesystem: false
        - name: rabbitmq-exporter
          image: kbudde/rabbitmq-exporter
          imagePullPolicy: IfNotPresent
          ports:
            - name: exporter
              containerPort: 9090
""",
    "27-user-db-dep.yaml": f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: user-db
  labels:
    name: user-db
  namespace: {namespace}
spec:
  replicas: 1
  selector:
    matchLabels:
      name: user-db
  template:
    metadata:
      labels:
        name: user-db
    spec:
      containers:
        - name: user-db
          image: weaveworksdemos/user-db:0.3.0
          imagePullPolicy: IfNotPresent
          ports:
            - name: mongo
              containerPort: 27017
          volumeMounts:
            - mountPath: /tmp
              name: users-db-temp-vol
            - mountPath: /data/db-users
              name: users-db-vol
      volumes:
        - name: users-db-temp-vol
          emptyDir: {{}}
        - name: users-db-vol
          emptyDir: {{}}
""",
    "23-shipping-dep.yaml": f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: shipping
  labels:
    name: shipping
  namespace: {namespace}
spec:
  replicas: 1
  selector:
    matchLabels:
      name: shipping
  template:
    metadata:
      labels:
        name: shipping
    spec:
      containers:
        - name: shipping
          image: weaveworksdemos/shipping:0.4.8
          imagePullPolicy: IfNotPresent
          env:
            - name: ZIPKIN
              value: zipkin.jaeger.svc.cluster.local
            - name: JAVA_OPTS
              value: -Xms64m -Xmx128m -XX:+UseG1GC -Djava.security.egd=file:/dev/urandom -Dspring.zipkin.enabled=false
          resources:
            limits:
              cpu: 300m
              memory: 1Gi
            requests:
              cpu: 100m
              memory: 512Mi
          ports:
            - containerPort: 80
          startupProbe:
            tcpSocket:
              port: 80
            failureThreshold: 60
            periodSeconds: 5
            timeoutSeconds: 1
          readinessProbe:
            tcpSocket:
              port: 80
            periodSeconds: 5
            failureThreshold: 6
            timeoutSeconds: 1
          livenessProbe:
            tcpSocket:
              port: 80
            periodSeconds: 10
            failureThreshold: 3
            timeoutSeconds: 1
          securityContext:
            capabilities:
              add: ["NET_BIND_SERVICE"]
              drop: ["all"]
            readOnlyRootFilesystem: true
          volumeMounts:
            - mountPath: /tmp
              name: tmp-volume
      volumes:
        - name: tmp-volume
          emptyDir: {{}}
""",
    "25-user-dep.yaml": f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: user
  labels:
    name: user
  namespace: {namespace}
spec:
  replicas: 1
  selector:
    matchLabels:
      name: user
  template:
    metadata:
      labels:
        name: user
    spec:
      containers:
        - name: user
          image: weaveworksdemos/user:0.4.7
          imagePullPolicy: IfNotPresent
          env:
            - name: mongo
              value: user-db:27017
            - name: HATEAOS
              value: user
            - name: USER_DATABASE
              value: mongodb
          resources:
            limits:
              cpu: 300m
              memory: 200Mi
            requests:
              cpu: 100m
              memory: 100Mi
          ports:
            - containerPort: 80
          livenessProbe:
            httpGet:
              path: /health
              port: 80
            initialDelaySeconds: 30
            periodSeconds: 15
          readinessProbe:
            httpGet:
              path: /health
              port: 80
            initialDelaySeconds: 20
            periodSeconds: 3
""",
}

for filename, content in overrides.items():
    (base / filename).write_text(content, encoding="utf-8")
PY
}

wait_for_rollouts() {
    local deployments
    if [ "$SOCKSHOP_COMPONENT_SET" = "browse" ]; then
        deployments=(catalogue catalogue-db front-end session-db)
    else
        deployments=(
            carts carts-db catalogue catalogue-db front-end orders orders-db
            payment queue-master rabbitmq session-db shipping user user-db
        )
    fi

    for deploy_name in "${deployments[@]}"; do
        "$KUBECTL" rollout status "deployment/${deploy_name}" -n "$APP_NS" --timeout=240s
    done
}

main() {
    require_cmd git
    require_cmd python3
    require_cmd "$KUBECTL"

    prepare_repo
    patch_repo_for_k3s

    "$KUBECTL" get namespace "$APP_NS" >/dev/null 2>&1 || "$KUBECTL" create namespace "$APP_NS"
    "$KUBECTL" apply -k "${SOCKSHOP_DIR}/manifests/overlays/multi"
    if [ "$SOCKSHOP_COMPONENT_SET" = "browse" ]; then
        for name in carts carts-db orders orders-db payment queue-master rabbitmq shipping user user-db; do
            "$KUBECTL" delete deployment "$name" -n "$APP_NS" --ignore-not-found=true || true
            "$KUBECTL" delete service "$name" -n "$APP_NS" --ignore-not-found=true || true
        done
        "$KUBECTL" delete pvc carts-db-temp-pvc orders-db-temp-pvc user-db-temp-pvc -n "$APP_NS" --ignore-not-found=true || true
    fi
    if [ -f "$GUARDRAILS_FILE" ]; then
        sed "s/namespace: sock-shop/namespace: ${APP_NS}/g" "$GUARDRAILS_FILE" | "$KUBECTL" apply -f -
    fi
    wait_for_rollouts
    "$KUBECTL" get deploy -n "$APP_NS"
}

main "$@"
