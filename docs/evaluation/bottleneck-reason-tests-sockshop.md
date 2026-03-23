# Sock Shop Bottleneck Type Tests

## Purpose
This phase keeps the synthetic bottleneck-reason tests intact and adds a separate Sock Shop observation set.

It checks whether the framework can classify the practical top-level bottleneck type on real Sock Shop paths:

- `local_bottleneck`
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
| `SSBR1_local_bottleneck_catalogue` | `GET /catalogue` | `catalogue` | `catalogue` | `local_bottleneck` |
| `SSBR2_downstream_delay_customers` | `GET /customers` | `front-end`, `user` | `user` | `downstream_delay` |
| `SSBR3_external_or_unmonitored_customers` | `GET /customers` | `front-end` | `front-end` | `external_or_unmonitored_delay` |

## Result Location
- per-case results: `results/bottleneck_identification/sockshop_types/<case-name>/`
- phase table: `results/bottleneck_identification/sockshop_types/phase_summary.md`

## Run

```bash
bash src/scripts/bottleneck_identification_sockshop/run_bottleneck_identification_sockshop.sh all observation
```
