# Bottleneck Service Tests

## Purpose
This phase validates bottleneck service identification only.

It checks whether ThriveScale can:

- find the correct bottleneck service
- follow the correct monitored path using graph plus latency and dependency evidence
- do so under fixed resources, fixed replicas, and route-isolated traffic

This phase is observation only. It is not for autoscaling behavior yet.

## Test Mode
- namespace: `thrive-demo`
- mode: `observation`
- autoscaling disabled
- fixed replicas: `1`
- one route only per case
- state reset before every case
- test duration: `90s`
- cool-off after reset: `30-45s`

## Resource Baseline
- `gateway`: CPU `100m / 500m`, Memory `128Mi / 256Mi`
- `svc-cpu`: CPU `100m / 200m`, Memory `128Mi / 256Mi`
- `svc-io`: CPU `100m / 300m`, Memory `128Mi / 256Mi`
- `svc-chain`: CPU `100m / 300m`, Memory `128Mi / 256Mi`
- `svc-fanout`: CPU `150m / 400m`, Memory `128Mi / 256Mi`
- `svc-net`: CPU `100m / 300m`, Memory `128Mi / 256Mi`

Replica bounds:
- `minReplicas = 1`
- `maxReplicas = 5`

## Threshold Guidance
- `RUNQ_FIXED_THRESHOLD_MS = 3.0`
- `RUNQ_BORDERLINE_MS = 2.5`
- `CPU_THROTTLE_RATIO_THRESHOLD = 0.05`
- local dominant if `local_fraction >= 0.4`
- dependency dominant if `dependency_fraction >= 0.5`

## SLO Setup
Downstream `ServiceSLO` values:
- `svc-cpu = 10ms`
- `svc-chain = 12ms`
- `svc-io = 100ms`
- `svc-fanout = 100ms`

Gateway SLO is patched per case:
- CPU path cases: `gateway-slo = 10ms`
- chain path cases: `gateway-slo = 12ms`
- IO path cases: `gateway-slo = 100ms`
- fanout path cases: `gateway-slo = 100ms`

## Cases
| Case | Route Shape | Expected Service | Expected Path Reason | Expected Final Reason |
| --- | --- | --- | --- | --- |
| `BS1_local_cpu_service` | `gateway -> svc-cpu` | `svc-cpu` | `downstream_delay` | `local_cpu_pressure` |
| `BS2_local_io_service` | `gateway -> svc-io` | `svc-io` | `downstream_delay` | `local_cpu_pressure` |
| `BS3_chain_downstream_service` | `gateway -> svc-chain -> svc-cpu` | `svc-cpu` | `downstream_delay` | `local_cpu_pressure` |
| `BS4_fanout_dominant_child` | `gateway -> svc-fanout -> svc-cpu + svc-io` | `svc-io` | `downstream_delay` | `local_cpu_pressure` |

## Outputs Saved
For each case:
- patched gateway SLO used
- observed graph edges
- client p90 and client RPS
- platform p90
- truth p90 if available
- `service_handling_latency`
- `dependency_attributed_latency`
- `external_wait_latency`
- `runq_p90_latency`
- `cpu_throttle_ratio` if available
- detected bottleneck service
- detected path reason
- detected final reason
- expected vs actual
- pass/fail

## Result Location
- per-case results: `results/bottleneck_service/<case-name>/`
- phase table: `results/bottleneck_service/phase_summary.md`

## Run
Single case:

```bash
bash src/scripts/bottleneck_service/run_bottleneck_service.sh BS1_local_cpu_service observation
```

All bottleneck service cases:

```bash
bash src/scripts/bottleneck_service/run_bottleneck_service.sh all observation
```
