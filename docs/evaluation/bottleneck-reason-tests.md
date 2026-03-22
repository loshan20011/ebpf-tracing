# Bottleneck Reason Tests

## Purpose
This phase validates bottleneck reason classification after service identification.

It checks whether ThriveScale can:

- identify the final bottleneck service correctly
- classify the bottleneck reason correctly
- keep path reason and final reason separate
- do so under fixed resources and route-isolated observation traffic

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

## Reason Classes
- `downstream_delay`
- `local_cpu_pressure`
- `local_unclear_or_non_cpu`
- `external_or_unmonitored_delay`

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
- net / external-delay case: `gateway-slo = 100ms`

## Cases
| Case | Scenario | Expected Service | Expected Path Reason | Expected Final Reason |
| --- | --- | --- | --- | --- |
| `BR1_local_cpu_pressure` | direct CPU pressure on `svc-cpu` | `svc-cpu` | `downstream_delay` | `local_cpu_pressure` |
| `BR2_downstream_delay` | monitored child behind `svc-chain` | `svc-cpu` | `downstream_delay` | `local_cpu_pressure` |
| `BR3_external_delay` | external dependency behavior behind `svc-net` | `svc-net` | `downstream_delay` | `external_or_unmonitored_delay` |
| `BR4_local_unclear_non_cpu` | local non-CPU delay on `svc-io` | `svc-io` | `downstream_delay` | `local_unclear_or_non_cpu` |

## Pass / Fail Semantics
Use the last `40s` of the run.

Report separately:
- graph pass
- service identification pass
- path reason pass
- final reason pass
- overall pass

Do not collapse path reason and final reason into one ambiguous field.

## Outputs Saved
For each case:
- patched gateway SLO used
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
- per-case results: `results/functional/bottleneck_reason/<case-name>/`
- phase table: `results/functional/bottleneck_reason/phase_summary.md`

## Run
Single case:

```bash
python3 src/scripts/evaluation/run_bottleneck_case.py \
  --case-config src/scripts/evaluation/bottleneck_reason/cases/BR1_local_cpu_pressure.json \
  --output-root results/functional/bottleneck_reason \
  --mode observation
```

All bottleneck reason cases:

```bash
bash src/scripts/evaluation/run_bottleneck_phases.sh reason observation
```
