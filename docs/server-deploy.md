# Server Deploy Runbook

This note captures the practical server-side commands used to deploy the current generic ThriveScale runtime on the k3s host.

Server:

- `ubuntu@54.82.19.113`
- repo path: `/home/ubuntu/thrive-scale-src-20260321`
- system namespace: `thrive-scale`
- app namespace in current setup: `sock-shop`

## 1. Sync Runtime Files

From the local workspace:

```bash
scp -i ~/Downloads/dev-stage-test.pem \
  src/controller/controller.py \
  src/aggregator/aggregator.py \
  src/aggregator/aggregator_benchmark.py \
  src/agent/agent.py \
  README.md \
  docs/runtime-hardcoding-audit.md \
  ubuntu@54.82.19.113:/home/ubuntu/thrive-scale-src-20260321/
```

Then on the server:

```bash
ssh -i ~/Downloads/dev-stage-test.pem ubuntu@54.82.19.113
cd /home/ubuntu/thrive-scale-src-20260321

mv controller.py src/controller/controller.py
mv aggregator.py src/aggregator/aggregator.py
mv aggregator_benchmark.py src/aggregator/aggregator_benchmark.py
mv agent.py src/agent/agent.py
mv runtime-hardcoding-audit.md docs/runtime-hardcoding-audit.md
```

Optional syntax check:

```bash
python3 -m py_compile \
  src/controller/controller.py \
  src/aggregator/aggregator.py \
  src/aggregator/aggregator_benchmark.py \
  src/aggregator/aggregator_metrics.py \
  src/agent/agent.py
```

## 2. Rebuild Aggregator Image

On the server:

```bash
cd /home/ubuntu/thrive-scale-src-20260321

docker build \
  -t thrive-local/aggregator:dynamic-20260325 \
  -f src/aggregator/Dockerfile \
  src/aggregator

docker save thrive-local/aggregator:dynamic-20260325 | sudo k3s ctr images import -
```

Roll out:

```bash
kubectl -n thrive-scale set image \
  deploy/aggregator \
  aggregator=thrive-local/aggregator:dynamic-20260325

kubectl -n thrive-scale set env deploy/aggregator \
  SERVICE_LABEL_KEYS=app.kubernetes.io/name,app,name \
  FUNCTIONAL_TEST_MODE=true \
  EBPF_REQ_MIN_COUNT=5 \
  EBPF_REQ_MIN_NET_SAMPLES=5 \
  EBPF_REQ_MIN_RUNQ_SAMPLES=5 \
  EBPF_REQ_MIN_RPS=0.25 \
  CPU_THROTTLE_RATIO_THRESHOLD=0.10

kubectl -n thrive-scale rollout restart deploy/aggregator
kubectl -n thrive-scale rollout status deploy/aggregator --timeout=300s
```

## 3. Rebuild Agent Image

On the server:

```bash
cd /home/ubuntu/thrive-scale-src-20260321

docker build \
  -t thrive-local/bpf-agent:dynamic-20260325 \
  -f src/agent/Dockerfile \
  src/agent

docker save thrive-local/bpf-agent:dynamic-20260325 | sudo k3s ctr images import -
```

Roll out:

```bash
kubectl -n thrive-scale set image \
  ds/bpf-agent \
  bpf-agent=thrive-local/bpf-agent:dynamic-20260325

kubectl -n thrive-scale set env ds/bpf-agent \
  SERVICE_LABEL_KEYS=app.kubernetes.io/name,app,name

kubectl -n thrive-scale rollout restart ds/bpf-agent
kubectl -n thrive-scale rollout status ds/bpf-agent --timeout=300s
```

## 4. Refresh Controller Override

The controller is mounted from the live ConfigMap instead of rebuilding the image.

```bash
kubectl -n thrive-scale create configmap controller-override \
  --from-file=controller.py=/home/ubuntu/thrive-scale-src-20260321/src/controller/controller.py \
  -o yaml --dry-run=client | kubectl apply -f -
```

Remove stale explicit root override and keep the controller generic:

```bash
kubectl -n thrive-scale set env deploy/custom-autoscaler ROOT_SERVICE-
```

Remove stale controller envs that are no longer used by the current code:

```bash
kubectl -n thrive-scale set env deploy/custom-autoscaler \
  STRONG_DOWNSCALE_STREAK_REQUIRED- \
  SECONDARY_LOCAL_PRESSURE_STREAK_REQUIRED- \
  LATENCY_ONLY_LOCAL_PRESSURE_MIN_RPS- \
  PRIMARY_UPSCALE_COOLDOWN_S-
```

Restart:

```bash
kubectl -n thrive-scale rollout restart deploy/custom-autoscaler
kubectl -n thrive-scale rollout status deploy/custom-autoscaler --timeout=300s
```

## 5. Verify Runtime Health

```bash
kubectl get deploy/aggregator deploy/custom-autoscaler -n thrive-scale -o wide
kubectl get ds/bpf-agent -n thrive-scale -o wide
kubectl get pods -n thrive-scale -o wide
```

Useful logs:

```bash
kubectl logs -n thrive-scale deploy/aggregator --tail=40
kubectl logs -n thrive-scale deploy/custom-autoscaler --tail=40
kubectl logs -n thrive-scale ds/bpf-agent --tail=40
```

Healthy signs:

- aggregator serves `/api/graph`
- controller emits decision traces instead of connection errors
- bpf-agent reports parsed events and no crash loop

## 6. Generic Runtime Configuration

Current important runtime envs:

- `TARGET_NAMESPACE`
- `SERVICE_LABEL_KEYS`
- `AGGREGATOR_URL`
- `ROOT_SERVICE` only if explicit override is needed

Preferred service identity label order currently used:

```bash
SERVICE_LABEL_KEYS=app.kubernetes.io/name,app,name
```

If your application uses different labels, change that value on both:

- `deploy/aggregator`
- `ds/bpf-agent`

## 7. Notes

- `aggregator_benchmark.py` still contains benchmark helper presets, but they only matter if `BENCHMARK_PROFILE` is explicitly set.
- The runtime path itself is intended to stay generic now.
- For generic apps, define clear `ServiceSLO` objects and either:
  - set one `priority: primary`, or
  - set `ROOT_SERVICE` explicitly if root inference is ambiguous.
