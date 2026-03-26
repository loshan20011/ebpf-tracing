Metric and path-validation scripts:

- `run_functional_case.py`: run one low-rate functional/path case from `cases/`.
- `summarize_functional_case.py`: summarize a functional run.
- `run_graph_checks.sh`: run the four route graph-check cases and print readable summaries.
- `run_metric_checks.sh`: run only the metric-accurate validation paths with short summaries.

Cases:

- `cases/baseline_low_steady.json`
- `cases/catalogue_only_low.json`
- `cases/login_only_low.json`
- `cases/cart_get_only_low.json`
- `cases/customers_only_low.json`

Example:

```bash
python3 src/scripts/metrics/run_functional_case.py \
  --config src/scripts/metrics/cases/login_only_low.json \
  --output-root results/metrics
```

Graph-check commands:

```bash
bash src/scripts/metrics/run_graph_checks.sh all
bash src/scripts/metrics/run_graph_checks.sh catalogue
bash src/scripts/metrics/run_graph_checks.sh login
bash src/scripts/metrics/run_graph_checks.sh cart_get
bash src/scripts/metrics/run_graph_checks.sh customers
```

Metric-check commands:

```bash
bash src/scripts/metrics/run_metric_checks.sh all
bash src/scripts/metrics/run_metric_checks.sh baseline
bash src/scripts/metrics/run_metric_checks.sh catalogue
bash src/scripts/metrics/run_metric_checks.sh cart_get
bash src/scripts/metrics/run_metric_checks.sh customers
```

Recommended metric-validation paths:

- `baseline_low_steady`
- `F1_graph_catalogue`
- `F2_graph_cart`
- `F3_graph_customers`

Note:

- the login route remains available under `run_graph_checks.sh login`
- the metric-check suite intentionally skips the login case for now because that route has been unstable in the low-rate validation flow
