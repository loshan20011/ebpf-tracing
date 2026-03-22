# Bottleneck Service Tests

## Purpose
Phase 1 verifies that ThriveScale identifies the correct bottleneck service before controller-action validation.

The bottleneck service checks also record the detected reason class so downstream-vs-local mismatches are visible early.

## Cases
| Case | Route Shape | Injection Style | Expected Bottleneck Service |
| --- | --- | --- | --- |
| `BS1_local_cpu_service` | `gateway -> svc-cpu` | high CPU iterations on `svc-cpu` via `/cpu?count=15000000` | `svc-cpu` |
| `BS2_local_io_service` | `gateway -> svc-io` | inherent I/O delay on `svc-io` via `/io` | `svc-io` |
| `BS3_chain_downstream_service` | `gateway -> svc-chain -> svc-cpu` | slow monitored downstream child via `/chain?count=15000000` | `svc-cpu` |
| `BS4_fanout_dominant_child` | `gateway -> svc-fanout -> svc-cpu + svc-io` | make `svc-io` dominate fanout via `/fanout?count=500000` | `svc-io` |

## Collected Outputs
- client p90 latency and success RPS
- observed graph edges
- platform p90 and truth p90 for root/path services
- detected bottleneck service
- expected vs actual bottleneck service
- pass/fail summary

## Result Location
- per-case results: `results/functional/bottleneck_service/<case-name>/`
- phase table: `results/functional/bottleneck_service/phase_summary.md`

## Run
Observation mode:

```bash
python src/scripts/evaluation/run_bottleneck_case.py ^
  --case-config src/scripts/evaluation/bottleneck_service/cases/BS1_local_cpu_service.json ^
  --output-root results/functional/bottleneck_service
```

All service-identification cases:

```bash
bash src/scripts/evaluation/run_bottleneck_phases.sh service observation
```

## Summary Table
The phase summary table is generated automatically in:

- `results/functional/bottleneck_service/phase_summary.md`
- `results/functional/bottleneck_service/phase_summary.json`
