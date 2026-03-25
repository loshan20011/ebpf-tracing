# Sock Shop Bottleneck Type Tests

## Purpose
This is the final likely-bottleneck service/reason evaluation set for Sock Shop.

It checks whether the framework can classify the practical top-level bottleneck type on real Sock Shop paths using only three cases:

- `local_cpu_pressure`
- `downstream_delay`
- `external_or_unmonitored_delay`

## Test Mode
- namespace: `sock-shop`
- mode: `observation`
- autoscaling disabled
- fixed replicas: `1`
- one route only per case
- short steady traffic per case

## Cases
| Case | Route | Monitored Services | Expected Service | Expected Type |
| --- | --- | --- | --- | --- |
| `SSBR1_local_bottleneck_catalogue` | `GET /catalogue` | `catalogue` | `catalogue` | `local_cpu_pressure` |
| `SSBR2_downstream_delay_customers` | `GET /customers` | `front-end`, `user` | `user` | `downstream_delay` |
| `SSBR3_external_or_unmonitored_customers` | `GET /customers` | `front-end` | `front-end` | `external_or_unmonitored_delay` |

Do not use the older synthetic bottleneck-reason cases for this final Sock Shop likely-bottleneck evaluation.

## Result Location
- per-case results: `results/bottleneck_identification/sockshop_types/<case-name>/`
- phase table: `results/bottleneck_identification/sockshop_types/phase_summary.md`

## Run

```bash
bash src/scripts/bottleneck_identification_sockshop/run_bottleneck_identification_sockshop.sh all observation
```
