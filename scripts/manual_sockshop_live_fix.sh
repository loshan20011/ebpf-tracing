#!/usr/bin/env bash
set -euo pipefail

KUBECTL="${KUBECTL:-kubectl}"
APP_NS="${APP_NS:-sock-shop}"

"$KUBECTL" patch deploy payment -n "$APP_NS" --type='strategic' -p '
spec:
  template:
    spec:
      containers:
      - name: payment
        command: null
        args: null
        ports:
        - containerPort: 80
        livenessProbe:
          httpGet:
            path: /health
            port: 80
          initialDelaySeconds: 30
          periodSeconds: 5
        readinessProbe:
          httpGet:
            path: /health
            port: 80
          initialDelaySeconds: 20
          periodSeconds: 5
'

"$KUBECTL" patch deploy shipping -n "$APP_NS" --type='strategic' -p '
spec:
  template:
    spec:
      containers:
      - name: shipping
        ports:
        - containerPort: 80
        startupProbe:
          tcpSocket:
            port: 80
          failureThreshold: 90
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
'

"$KUBECTL" patch deploy user -n "$APP_NS" --type='strategic' -p '
spec:
  template:
    spec:
      containers:
      - name: user
        ports:
        - containerPort: 80
        readinessProbe:
          httpGet:
            path: /health
            port: 80
          initialDelaySeconds: 20
          periodSeconds: 3
        livenessProbe:
          httpGet:
            path: /health
            port: 80
          initialDelaySeconds: 30
          periodSeconds: 15
'

"$KUBECTL" patch deploy carts -n "$APP_NS" --type='strategic' -p '
spec:
  template:
    spec:
      containers:
      - name: carts
        env:
        - name: JAVA_OPTS
          value: -Xms64m -Xmx256m -XX:+UseG1GC -Djava.security.egd=file:/dev/urandom -Dspring.zipkin.enabled=false
        ports:
        - containerPort: 80
        resources:
          requests:
            cpu: 100m
            memory: 300Mi
          limits:
            cpu: 500m
            memory: 1Gi
        startupProbe:
          tcpSocket:
            port: 80
          failureThreshold: 120
          periodSeconds: 5
          timeoutSeconds: 1
        readinessProbe:
          tcpSocket:
            port: 80
          failureThreshold: 6
          periodSeconds: 5
          timeoutSeconds: 1
        livenessProbe:
          tcpSocket:
            port: 80
          failureThreshold: 3
          periodSeconds: 10
          timeoutSeconds: 1
'

"$KUBECTL" patch deploy orders -n "$APP_NS" --type='strategic' -p '
spec:
  template:
    spec:
      containers:
      - name: orders
        env:
        - name: JAVA_OPTS
          value: -Xms64m -Xmx256m -XX:+UseG1GC -Djava.security.egd=file:/dev/urandom -Dspring.zipkin.enabled=false
        ports:
        - containerPort: 80
        resources:
          requests:
            cpu: 100m
            memory: 400Mi
          limits:
            cpu: 700m
            memory: 1Gi
        startupProbe:
          tcpSocket:
            port: 80
          failureThreshold: 120
          periodSeconds: 5
          timeoutSeconds: 1
        readinessProbe:
          tcpSocket:
            port: 80
          failureThreshold: 6
          periodSeconds: 5
          timeoutSeconds: 1
        livenessProbe:
          tcpSocket:
            port: 80
          failureThreshold: 3
          periodSeconds: 10
          timeoutSeconds: 1
'

"$KUBECTL" rollout restart deploy/payment deploy/shipping deploy/user deploy/carts deploy/orders -n "$APP_NS"
"$KUBECTL" rollout status deploy/payment -n "$APP_NS" --timeout=240s || true
"$KUBECTL" rollout status deploy/shipping -n "$APP_NS" --timeout=240s || true
"$KUBECTL" rollout status deploy/user -n "$APP_NS" --timeout=240s || true
"$KUBECTL" rollout status deploy/carts -n "$APP_NS" --timeout=240s || true
"$KUBECTL" rollout status deploy/orders -n "$APP_NS" --timeout=240s || true
"$KUBECTL" get pods -n "$APP_NS"
