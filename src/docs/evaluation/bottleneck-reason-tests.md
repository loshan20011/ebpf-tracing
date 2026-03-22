# Bottleneck Reason Tests

## Purpose
Phase 2 verifies whether ThriveScale classifies the bottleneck reason correctly before control-mode validation.

## Reason Classes
- `local_cpu_pressure`
- `downstream_delay`
- `external_or_unmonitored_delay`
- `local_unclear_or_non_cpu`

## Cases
| Case | Scenario | Expected Bottleneck Service | Expected Reason |
| --- | --- | --- | --- |
| `BR1_local_cpu_pressure` | direct CPU pressure on `svc-cpu` | `svc-cpu` | `local_cpu_pressure` |
| `BR2_downstream_delay` | slow monitored child behind `svc-chain` | `svc-cpu` | `downstream_delay` |
| `BR3_external_delay` | external dependency behind `svc-net` | `svc-net` | `external_or_unmonitored_delay` |
| `BR4_local_unclear_non_cpu` | local delay without strong runq rise on `svc-io` | `svc-io` | `local_unclear_or_non_cpu` |

## Collected Outputs
- client p90 latency and success RPS
- `service_handling_latency`
- `dependency_attributed_latency`
- `external_wait_latency`
- `runq_p90_latency`
- detected bottleneck service
- detected reason class
- expected vs actual reason
- pass/fail summary

## Result Location
- per-case results: `results/functional/bottleneck_reason/<case-name>/`
- phase table: `results/functional/bottleneck_reason/phase_summary.md`

## Run
Observation mode:

```bash
python src/scripts/evaluation/run_bottleneck_case.py ^
  --case-config src/scripts/evaluation/bottleneck_reason/cases/BR1_local_cpu_pressure.json ^
  --output-root results/functional/bottleneck_reason
```

All reason-classification cases:

```bash
bash src/scripts/evaluation/run_bottleneck_phases.sh reason observation
```

## Next Step
After both observation phases are complete:
1. rerun key cases in control mode
2. verify controller decision correctness
3. only then move on to Sock Shop scenarios or HPA benchmarks
