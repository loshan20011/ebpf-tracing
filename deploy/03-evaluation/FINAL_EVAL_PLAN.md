# Final HPA vs ThriveScale Evaluation Plan

## Goal

Run a final, defensible comparison between `ThriveScale` and Kubernetes `HPA` using:

- a clean and reproducible K3s environment
- a complete Sock Shop deployment
- paper-grounded thresholds and safeguards
- WorldCup trace-driven traffic
- consistent cost and SLO-violation analysis

## Paper-Grounded Principles

### From `Key Considerations for Auto-Scaling: Lessons from Benchmark Microservices`

- use the full service chain, not a partial benchmark deployment
- configure readiness and liveness probes carefully
- keep resource requests and limits bounded
- avoid hidden startup artifacts and bulk scale-out contention
- evaluate dependencies, not just isolated services
- ensure failure visibility and downstream observability

### From `STaleX`

- compare against HPA using the same environment and workload
- avoid a single global threshold for all services
- use differentiated per-service thresholds
- evaluate both SLO violations and resource cost
- use WorldCup98 trace-based traffic rather than ad hoc synthetic mixes
- the paper's benchmarked chain is `/login` across `front-end`, `user`, and `carts`
- the paper reports a `200 ms` SLO and these HPA threshold sets:
  - uniform `25/25/25`
  - uniform `50/50/50`
  - uniform `60/60/60`
  - uniform `70/70/70`
  - differentiated `50/50/25`
  - differentiated `70/70/40`
  - differentiated `70/80/50`
  - differentiated `40/60/70`

## Final Environment

### Cluster

- use one clean K3s cluster on the evaluation node
- remove stale namespaces, old Jobs, and leftover HPA/ThriveScale objects before each campaign
- keep the EC2 host-side public frontend out of the evaluation path
- run evaluation against in-cluster NodePorts or ClusterIPs only

### Namespaces

- `sock-shop` for the benchmark
- `thrive-scale` for the autoscaling stack

### Sock Shop Deployment

Deploy all relevant Sock Shop services for a realistic chain:

- `front-end`
- `catalogue`
- `catalogue-db`
- `user`
- `user-db`
- `carts`
- `carts-db`
- `orders`
- `orders-db`
- `payment`
- `shipping`
- `queue-master`
- `rabbitmq`

### Guardrails

- keep requests and limits on all services
- keep probe settings explicit and stable
- keep the fixed `catalogue` Service port mapping (`targetPort: 80`)
- pin image tags during final experiments
- clear Redis truth/history state between runs

## Threshold Methodology

### ThriveScale SLOs

Use route/service SLOs derived from the benchmarked chain and prior stable measurements, not arbitrary values.

Recommended rule:

1. Run a low-load calibration with full Sock Shop deployed.
2. Measure stable P90 for target services.
3. Set the final SLO to a paper-defensible target:
   - either a route-level target justified by prior evaluation docs
   - or approximately `2x-3x` the stable low-load P90, rounded to a practical threshold

### HPA Thresholds

Do not use one CPU threshold for every service.

Use a calibration sweep first:

- uniform thresholds: `25`, `50`, `60`, `70`
- differentiated thresholds for hot services after the sweep

Final HPA baseline should use the best threshold set chosen from calibration, then frozen before final comparison.

For the STaleX-aligned evaluation in this repo, use the paper's exact service set and threshold sweep files under `deploy/03-evaluation/`:

- `hpa-sockshop-stalex-uniform-25.yaml`
- `hpa-sockshop-stalex-uniform-50.yaml`
- `hpa-sockshop-stalex-uniform-60.yaml`
- `hpa-sockshop-stalex-uniform-70.yaml`
- `hpa-sockshop-stalex-diff-50-50-25.yaml`
- `hpa-sockshop-stalex-diff-70-70-40.yaml`
- `hpa-sockshop-stalex-diff-70-80-50.yaml`
- `hpa-sockshop-stalex-diff-40-60-70.yaml`

## Traffic Methodology

### Source

Use the WorldCup trace already present in the repo:

- `deploy/03-evaluation/datasets/worldcup98_day75_peak_10m_10s.csv`
- original literature citation used by both papers: `M. Arlitt and T. Jin, A workload characterization study of the 1998 World Cup Web site, IEEE Network, 2000`
- raw dataset host used by this repo's extractor: `https://ita.ee.lbl.gov/traces/WorldCup`

### Replay

- use the exact trace-derived workload generator path
- do not use manual browse mixes for final comparison
- replay the same workload for HPA and ThriveScale
- use identical warmup, duration, timeout, and sample intervals

### Scenarios

Run at least:

1. `WorldCup browse/representative load`
2. `WorldCup peak burst`
3. optional `dependency-heavy chain` if thesis results need a downstream bottleneck case

## Experimental Procedure

For each controller (`HPA`, `ThriveScale`):

1. Reset environment.
2. Deploy the same Sock Shop stack.
3. Apply only the controller-specific manifests.
4. Warm up the application.
5. Run the WorldCup workload.
6. Collect:
   - autoscaler logs
   - graph snapshots
   - harness CSV
   - breach CSV
   - final replica history
7. Repeat each scenario at least `3` times.

## Metrics for Review

### SLO Metrics

- `violation_rate`
  - PBScaler-style metric
  - fraction or percentage of scored samples where end-to-end `P90 > SLO`
- `violation_count`
  - number of violating scored windows
- `violation_magnitude_ms`
  - STaleX-style metric
  - sum of `max(0, P90 - SLO)` across scored windows
- `time_to_first_recovery`
  - first time the service returns below SLO after a breach

For thesis reporting, both violation styles must be shown:

- PBScaler-aligned:
  - `violation_rate (%)`
- STaleX-aligned:
  - `violation_magnitude_ms`

This avoids over-committing to a single paper's reporting style and makes the comparison easier to defend.

### Cost Metrics

- `replica_seconds`
  - sum over time of ready replicas multiplied by window length
- `cpu_core_minutes`
  - if available from requests/limits or metrics server
- `peak_replica_count`

### Stability Metrics

- number of scale actions
- oscillation frequency
- average upscale step
- average downscale step

### Reliability Metrics

- `5xx_rate`
- `timeout_rate`
- `connect_error_rate`

## Deliverables

Keep only these categories in the final workspace:

- `src/`
- `deploy/`
- `scripts/`
- `tests/` if still needed
- `results/.gitkeep`
- final evaluation workload and dataset assets
- final summary docs

Do not retain old ad hoc run CSVs or extracted scratch files.

## Immediate Next Actions

1. Clean and redeploy full Sock Shop.
   Command: `bash scripts/reset_and_install_final_eval_env.sh`
2. Freeze final SLO manifests.
3. Freeze final HPA manifests.
4. Use the WorldCup trace-driven workload only.
5. Create one summary script for:
   - violation rate
   - violation magnitude
   - replica-seconds
   - peak replicas
   - comparative table for HPA vs ThriveScale
6. For the STaleX-style paper track, run:
   Command: `bash scripts/run_stalex_paper_eval.sh`
