Bottleneck identification scripts for Sock Shop:

- `run_bottleneck_identification_sockshop.sh`: run the final Sock Shop likely-bottleneck service/reason cases in this folder.
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
