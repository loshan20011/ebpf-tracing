Shared helpers:

- `collect_metrics.py`: collector and summarization driver used by multiple evaluation families.
- `patch_demo_gateway_slo.py`: patch ServiceSLO specs for demo and Sock Shop runs.
- `run_bottleneck_case.py`: shared bottleneck case runner.
- `summarize_bottleneck_case.py`: shared single-case bottleneck summary.
- `summarize_bottleneck_phase.py`: shared phase summary for bottleneck runs.

These are helper scripts. Prefer using the entrypoint scripts from the category folders unless you are debugging internals.
