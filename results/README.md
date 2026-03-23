Results layout:

- `results/benchmark/`: final benchmark outputs.
  - `results/benchmark/login/hpa50/`: frozen final HPA-50 `/login` benchmark bundle.
  - `results/benchmark/login/hpa75/`: frozen final HPA-75 `/login` benchmark bundle.
  - `results/benchmark/login/thrivescale/`: frozen final ThriveScale `/login` benchmark bundle.
- `results/metrics/`: baseline and path-validation runs, plus metric-focused exports and summaries.
- `results/bottleneck_identification/`: bottleneck-reason and path-identification outputs.
- `results/bottleneck_identification/sockshop_types/`: Sock Shop bottleneck-type outputs.
- `results/bottleneck_service/`: service-targeting evaluation outputs.
- `results/replica_count/`: replica growth, replica-seconds, and scaling timeline exports.

Keep raw run artifacts inside one of these top-level folders instead of creating new ad-hoc folders in the repository root.
