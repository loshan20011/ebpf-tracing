Script layout:

- `benchmark/`: final Sock Shop benchmark runners and login-user setup helpers.
  - `legacy/`: older helpers kept only for reference, not for the frozen final benchmark.
- `metrics/`: baseline and path-validation scripts.
- `bottleneck_identification/`: older synthetic bottleneck-reason entrypoints and cases.
- `bottleneck_identification_sockshop/`: final Sock Shop likely-bottleneck service/reason entrypoints and cases.
- `bottleneck_service/`: bottleneck-service entrypoints and cases.
- `control_loop/`: control-loop scripts and case files.
- `common/`: shared helper scripts used by multiple script families.

Use the `README.md` inside each folder for the expected commands.
