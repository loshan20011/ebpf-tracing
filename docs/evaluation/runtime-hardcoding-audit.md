# Runtime Hardcoding Audit

This note reviews hardcoding inside the main runtime folders:

- `src/agent`
- `src/aggregator`
- `src/controller`
- `src/frontend`

The goal is to separate:

- `Keep as intended`
- `Make configurable`
- `Remove as unnecessary`

| Area | File | Hardcoding | Why It Exists | Used For | Recommendation |
| --- | --- | --- | --- | --- | --- |
| Agent | `src/agent/agent.py` | `TARGET_NAMESPACE="default"` | Runtime namespace default for Kubernetes discovery | Metric attribution and service/IP mapping | Keep as intended |
| Agent | `src/agent/agent.py` | Dynamic pod/service label mapping via `SERVICE_LABEL_KEYS` | Generic workload discovery from Kubernetes metadata | Metric attribution, topology support | Keep as intended |
| Aggregator | `src/aggregator/aggregator.py` | `TARGET_NAMESPACE="default"` | Runtime namespace default | Graph/state collection, control APIs | Keep as intended |
| Aggregator | `src/aggregator/aggregator.py` | `TRAFFIC_TARGET_BASE_URL` | Optional dashboard traffic target | Traffic-control API support | Configured via env |
| Aggregator | `src/aggregator/aggregator.py` | Workload-name resolution via `SERVICE_LABEL_KEYS` | Generic pod/service to logical-service mapping | Graph/state collection | Keep as intended |
| Aggregator | `src/aggregator/aggregator.py` | Support ticket Redis key names | UI/operator feature storage | Frontend support desk | Keep as intended |
| Aggregator | `src/aggregator/aggregator_metrics.py` | Generic thresholds and window defaults | Runtime metric aggregation defaults, not service-name hardcoding | Metrics, evidence, topology support | Keep as intended |
| Aggregator | `src/aggregator/aggregator_benchmark.py` | Sock Shop/synthetic route presets | Optional benchmark profile metadata for dashboard traffic helpers | Frontend/benchmark support only | Keep as benchmark-only helper |
| Aggregator | `src/aggregator/app.py` | Alternate launcher wrapper | Duplicated startup path; Docker already runs `aggregator.py` directly | None in current runtime | Remove as unnecessary |
| Controller | `src/controller/controller.py` | `TARGET_NAMESPACE="default"` | Runtime namespace default | Scale reads/patches, ServiceSLO reads | Keep as intended |
| Controller | `src/controller/controller.py` | Root service selection | Control ordering and root protection | Root-trigger traversal and action ranking | Dynamic discovery first, explicit env override optional |
| Controller | `src/controller/controller.py` | Root-service-first protection logic | Generic once `ROOT_SERVICE` is configurable; no longer hardcodes downstream services | Bottleneck targeting and action ranking | Keep as intended |
| Frontend | `src/frontend/dashboard.html` | Fallback target text `"front-end"` | Display fallback when benchmark metadata is missing | UI only | Replaced with neutral fallback |
| Frontend | `src/frontend/dashboard.html` | Placeholder route `/catalogue` | Demo/default traffic form value | UI only | Replaced with neutral placeholder |
| Frontend | `src/frontend/dashboard.html` | `Likely Bottleneck`, `Support Desk`, ticket labels | Product/UI wording, not service-name logic | UI only | Keep as intended |

## Summary

### Keep as intended

- Namespace defaults in `agent.py`, `aggregator.py`, and `controller.py`
- Generic metric thresholds and windows in `aggregator_metrics.py`
- Root-service-aware controller behavior, as long as `ROOT_SERVICE` remains configurable
- Frontend support desk and trace terminology

### Make configurable

- Explicit benchmark profile selection in `aggregator_benchmark.py`
- Optional traffic base URL in `aggregator.py`
- Preferred service label keys in `agent.py` and `aggregator.py`

### Remove as unnecessary

- `src/aggregator/app.py`

`app.py` was redundant because:

- the container entrypoint is `python -u aggregator.py`
- `aggregator.py` already starts the Flask app directly
- nothing else in the repo references `app.py`

## Implemented Cleanup

- removed `src/aggregator/app.py`
- removed benchmark defaults from the controller runtime root-service selection
- made workload-name discovery configurable via:
  - `SERVICE_LABEL_KEYS`
- made `src/aggregator/aggregator_benchmark.py` explicit benchmark-only helper behavior via:
  - `BENCHMARK_PROFILE`
  - `SOCKSHOP_ENTRY_SERVICE`
  - `SOCKSHOP_TRAFFIC_ROUTES_JSON`
  - `SYNTHETIC_ENTRY_SERVICE`
  - `SYNTHETIC_SERVICE_PREFIX`
- replaced dashboard Sock Shop-specific traffic fallbacks with neutral UI defaults
