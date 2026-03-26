Bottleneck identification scripts for Sock Shop:

- `run_bottleneck_identification_sockshop.sh`: run the final Sock Shop likely-bottleneck service/reason cases in this folder.
- `run_autoscaler_scenarios_sockshop.sh`: run the same Sock Shop scenarios against `No Autoscaler`, `HPA-50`, `HPA-70`, and `ThriveScale`.
- `cases/`: the three Sock Shop observation cases only:
  - `SSBR1_local_bottleneck_catalogue`
  - `SSBR2_downstream_delay_customers`
  - `SSBR3_external_or_unmonitored_customers`
- results go to `results/bottleneck_identification/sockshop_types/`

Use this folder for the final likely-bottleneck evaluation. The older synthetic `bottleneck_identification/` cases are separate and should not be used for the final Sock Shop comparison.

Example:

```bash
bash src/scripts/bottleneck_identification_sockshop/run_bottleneck_identification_sockshop.sh all observation
```

Scenario-based autoscaling comparison:

```bash
bash src/scripts/bottleneck_identification_sockshop/run_autoscaler_scenarios_sockshop.sh all all
cat results/scenario_autoscaling/sockshop/scenario_comparison.md
```

Notes:

- the Sock Shop cases are front-end-triggered where appropriate, and the downstream/external cases restart workloads after each reset so dependency edges can be rediscovered cleanly
- summaries are intentionally reduced to the final detected service and one final reason class only:
  - `local_cpu_pressure`
  - `downstream_delay`
  - `external_or_unmonitored_delay`
- the autoscaler scenario runner compares observable scaling behavior across:
  - `No Autoscaler`
  - `HPA-50`
  - `HPA-70`
  - `ThriveScale`
