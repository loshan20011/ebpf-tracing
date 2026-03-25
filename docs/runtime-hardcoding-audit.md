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
| Agent | `src/agent/agent.py` | Dynamic pod/service label mapping | Not app-specific hardcoding; learns services from Kubernetes metadata | Metric attribution, topology support | Keep as intended |
| Aggregator | `src/aggregator/aggregator.py` | `TARGET_NAMESPACE="default"` | Runtime namespace default | Graph/state collection, control APIs | Keep as intended |
| Aggregator | `src/aggregator/aggregator.py` | `TRAFFIC_TARGET_BASE_URL="http://gateway"` | Default traffic target for synthetic demo mode | Traffic-control API support | Make configurable |
| Aggregator | `src/aggregator/aggregator.py` | Support ticket Redis key names | UI/operator feature storage | Frontend support desk | Keep as intended |
| Aggregator | `src/aggregator/aggregator_metrics.py` | Generic thresholds and window defaults | Runtime metric aggregation defaults, not service-name hardcoding | Metrics, evidence, topology support | Keep as intended |
| Aggregator | `src/aggregator/aggregator_benchmark.py` | Sock Shop detection by namespace/name | Chooses benchmark profile for dashboard traffic controls | Frontend/benchmark support only | Configured via env |
| Aggregator | `src/aggregator/aggregator_benchmark.py` | Hardcoded Sock Shop routes and target services | Provides friendly route list for dashboard traffic generation | Frontend/benchmark support only | Configured via env |
| Aggregator | `src/aggregator/aggregator_benchmark.py` | Synthetic demo detection by `gateway`/`svc-*` | Chooses demo benchmark profile | Frontend/benchmark support only | Keep as intended |
| Aggregator | `src/aggregator/app.py` | Alternate launcher wrapper | Duplicated startup path; Docker already runs `aggregator.py` directly | None in current runtime | Remove as unnecessary |
| Controller | `src/controller/controller.py` | `TARGET_NAMESPACE="default"` | Runtime namespace default | Scale reads/patches, ServiceSLO reads | Keep as intended |
| Controller | `src/controller/controller.py` | `ROOT_SERVICE="front-end"` | Default app entry/root for control ordering | Root-trigger traversal and root protection | Make configurable |
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

- Sock Shop route/profile mapping in `aggregator_benchmark.py`
- `ROOT_SERVICE` default choice in `controller.py`
- Synthetic/default traffic base URL in `aggregator.py`

### Remove as unnecessary

- `src/aggregator/app.py`

`app.py` was redundant because:

- the container entrypoint is `python -u aggregator.py`
- `aggregator.py` already starts the Flask app directly
- nothing else in the repo references `app.py`

## Implemented Cleanup

- removed `src/aggregator/app.py`
- made `src/aggregator/aggregator_benchmark.py` configurable via environment:
  - `SOCKSHOP_NAMESPACES`
  - `SOCKSHOP_ENTRY_SERVICE`
  - `SOCKSHOP_TRAFFIC_ROUTES_JSON`
  - `SYNTHETIC_ENTRY_SERVICE`
  - `SYNTHETIC_SERVICE_PREFIX`
- replaced dashboard Sock Shop-specific traffic fallbacks with neutral UI defaults
