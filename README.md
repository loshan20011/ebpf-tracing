# ThriveScale Runtime Guide

ThriveScale is an eBPF-assisted Kubernetes autoscaling prototype made of four runtime components:

- `src/agent`: node-level event collector
- `src/aggregator`: metrics, topology, and control API service
- `src/controller`: autoscaler control loop
- `src/frontend`: dashboard and operator UI

This runtime is now designed to work generically for arbitrary Kubernetes applications, not only Sock Shop or synthetic demos.

## Runtime Model

The main runtime path is:

1. `agent` maps kernel events to Kubernetes services dynamically.
2. `aggregator` scrapes agent events, builds service metrics/topology, and exposes `/api/graph`.
3. `controller` reads `ServiceSLO` objects plus aggregator graph data and patches deployment replicas.
4. `frontend` reads aggregator APIs for visualization and operator control.

Benchmark-specific helpers still exist, but they are isolated to benchmark scripts and explicit benchmark helper logic.

## Required Inputs

### 1. Namespace

All runtime components operate against a target namespace:

- `TARGET_NAMESPACE`

### 2. Service Identity

The preferred service identity contract is label-based.

By default, the runtime resolves workload names from these labels in order:

- `app.kubernetes.io/name`
- `app`
- `name`

You can override that order with:

- `SERVICE_LABEL_KEYS`

Example:

```bash
SERVICE_LABEL_KEYS=app.kubernetes.io/name,app.kubernetes.io/instance,app,name
```

If no configured label is present, the runtime falls back to the Kubernetes object name.

### 3. ServiceSLO CRs

The controller expects `ServiceSLO` custom resources in the target namespace. Each monitored deployment should have:

- `targetDeployment`
- `sloLatency`
- `minReplicas`
- `maxReplicas`
- optional `priority`

Root-service selection is dynamic:

1. If `ROOT_SERVICE` is set, it is used explicitly.
2. Else if exactly one `ServiceSLO` has `priority: primary`, that service becomes the root.
3. Else the controller tries to infer a single root from topology.
4. Else it fails clearly and asks for explicit configuration.

## Key Runtime Environment Variables

### Shared / Common

- `TARGET_NAMESPACE`: namespace containing the monitored application
- `SERVICE_LABEL_KEYS`: comma-separated preferred label keys for service identity

### Agent

- `AGENT_PORT`
- `RAW_BUFFER_MAX_EVENTS`
- `RUNQ_MIN_US`
- `CPU_THROTTLE_POLL_SECONDS`
- `CPU_THROTTLE_MIN_RATIO`
- `CMDLINE_SERVICE_FALLBACK_ENABLED`

### Aggregator

- `AGENT_NAMESPACE`
- `AGENT_LABEL_SELECTOR`
- `SCRAPE_INTERVAL_SECONDS`
- `WINDOW_SHORT_SECONDS`
- `WINDOW_LONG_SECONDS`
- `METRIC_STALE_AFTER_SECONDS`
- `TOPOLOGY_STALE_AFTER_SECONDS`
- `REDIS_HOST`
- `REDIS_PORT`
- `CONTROL_API_TOKEN`
- `TRAFFIC_TARGET_BASE_URL`

### Controller

- `AGGREGATOR_URL`
- `AGGREGATOR_TIMEOUT_S`
- `LOOP_SECONDS`
- `ROOT_SERVICE` (optional explicit override)
- `ACTIVE_RPS_THRESHOLD`
- `UPSCALE_COOLDOWN_S`
- `DOWNSCALE_COOLDOWN_S`
- `RUNQ_FIXED_THRESHOLD_MS`
- `RECENT_BREACH_HOLD_S`
- `TRACE_LOGS`

### Frontend

- `AGGREGATOR_URL`

## Benchmark Helper Mode

Benchmark helper logic is isolated and optional.

The helper profile code lives in:

- `src/aggregator/aggregator_benchmark.py`

It activates only when explicitly configured with:

- `BENCHMARK_PROFILE=sock-shop`
- `BENCHMARK_PROFILE=synthetic`

Optional helper envs:

- `SOCKSHOP_ENTRY_SERVICE`
- `SOCKSHOP_TRAFFIC_ROUTES_JSON`
- `SYNTHETIC_ENTRY_SERVICE`
- `SYNTHETIC_SERVICE_PREFIX`

If `BENCHMARK_PROFILE` is unset, the runtime stays generic.

## Generic Deployment Notes

For a generic application:

1. Deploy the app into a namespace.
2. Add `ServiceSLO` objects for the deployments you want monitored/scaled.
3. Set `TARGET_NAMESPACE` on agent, aggregator, and controller.
4. Ensure workload labels match `SERVICE_LABEL_KEYS` or accept object-name fallback.
5. Set `ROOT_SERVICE` only if dynamic root inference is ambiguous.

## What Is No Longer Required

The runtime no longer silently depends on:

- `front-end`
- `gateway`
- `sock-shop`
- `svc-*`
- `mycurlpod`

Those names may still appear in explicit benchmark helper code, but not in the default runtime decision path.
