Bottleneck identification scripts:

- `run_bottleneck_identification.sh`: run the bottleneck-reason cases in this folder.
- `cases/`: reason-focused cases such as local CPU pressure, downstream delay, and external delay.
- results go to `results/bottleneck_identification/`

Example:

```bash
bash src/scripts/bottleneck_identification/run_bottleneck_identification.sh all observation
```
