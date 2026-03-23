# Bottleneck Identification Results

This folder stores bottleneck-type evaluation outputs.

Active result sets:

- synthetic bottleneck-reason phase:
  - run with `bash src/scripts/bottleneck_identification/run_bottleneck_identification.sh all observation`
  - outputs per case under `results/bottleneck_identification/<case-name>/`
  - phase summary files:
    - `results/bottleneck_identification/phase_summary.json`
    - `results/bottleneck_identification/phase_summary.md`
- Sock Shop bottleneck-type phase:
  - run with `bash src/scripts/bottleneck_identification_sockshop/run_bottleneck_identification_sockshop.sh all observation`
  - outputs per case under `results/bottleneck_identification/sockshop_types/<case-name>/`
  - phase summary files:
    - `results/bottleneck_identification/sockshop_types/phase_summary.json`
    - `results/bottleneck_identification/sockshop_types/phase_summary.md`

Do not create new placeholder subfolders here. Keep each phase under its real output root.
