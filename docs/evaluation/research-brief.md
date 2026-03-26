# ThriveScale Research Brief

## Purpose

This note summarizes the current repository, the evaluated runtime architecture, and the live k3s deployment inspected on `54.82.19.113` in read-only mode.

It is intended as a research aide, not as a deployment or implementation guide.

## Research Context

The thesis frames ThriveScale as an eBPF-driven, deterministic autoscaling system for Kubernetes that:

- collects low-overhead kernel-level signals using eBPF
- attributes latency and bottlenecks at service level
- reasons about service dependencies
- scales services according to Service Level Objectives rather than coarse CPU-only thresholds
- aims to be more explainable and lower overhead than black-box autoscaling methods

The repository largely supports that framing, but the current checked-in implementation is best understood as a prototype of an explainable, rule-based autoscaler rather than a full queueing-network optimizer.

## Repo Layout

### Core runtime components

- `src/agent/`: node-level metric collection logic
- `src/aggregator/`: central aggregation, topology, trace, and control API
- `src/controller/`: autoscaling decision logic
- `src/frontend/`: dashboard UI
- `deploy/`: Kubernetes setup and system manifests

### Evaluation assets

- `src/scripts/metrics/`: path and metric-validation runs
- `src/scripts/control_loop/`: control-loop behavior cases
- `src/scripts/bottleneck_identification/`: bottleneck-reason tests on synthetic workloads
- `src/scripts/bottleneck_identification_sockshop/`: bottleneck-type tests on Sock Shop
- `src/scripts/bottleneck_service/`: service-targeting tests
- `src/scripts/benchmark/`: final benchmark orchestration
- `results/`: frozen or recorded outputs from the above experiments

### Applications under study

- `src/test-workloads/`: synthetic microservice testbed
- `src/sockshop/`: Sock Shop deployment and app-specific helpers

## Implemented Architecture

### 1. Agent layer

The `bpf-agent` is deployed as a DaemonSet with `hostPID`, `hostNetwork`, and privileged access. It runs `bpftrace` probes and a Python wrapper to:

- map PIDs and network endpoints to Kubernetes services
- observe request-start and request-end timing via syscall traces
- observe outbound connect calls for dependency discovery
- observe scheduler wakeup and switch events for run-queue delay
- sample `cpu.stat` to estimate CPU throttling

The eBPF probe in `src/agent/sensor.bt` emits event types such as:

- `REQ`
- `NET`
- `CONN`
- `RUNQ`

The Python agent enriches these with Kubernetes metadata and serves them over HTTP for the aggregator.

### 2. Aggregation layer

The aggregator is a Flask service that:

- scrapes all agent pods on a short interval
- stores traces and audit events in Redis
- computes per-service latency, RPS, evidence confidence, and inferred topology
- exposes APIs such as `/api/graph`, `/api/traces`, `/api/control/state`, and support/control endpoints

Its output combines:

- eBPF-derived request evidence
- service graph inference
- optional truth-ingestion and benchmark helpers
- SLO definitions from the custom resource `ServiceSLO`

The aggregator is both the observability hub and the controller’s upstream data source.

### 3. Controller layer

The controller polls the aggregator and makes scaling decisions by:

- checking SLO breach status and sustained breach streaks
- measuring traffic activity and downscale windows
- identifying whether delay appears local, dependency-driven, external, or unclear
- using run-queue delay and CPU throttling as local resource pressure hints
- traversing dependency graphs to find likely root-cause services
- patching deployment replica counts in Kubernetes

This is a deterministic and explainable decision pipeline, but in the checked-in code it is heuristic. It does not appear to implement a separate queueing-network solver or formal optimization engine in the runtime path.

### 4. Frontend layer

The dashboard displays:

- service graph state
- service metrics
- decision traces
- SLO and scale controls
- support-ticket workflow artifacts

In the live cluster, the frontend is overridden by ConfigMaps, which suggests the deployed dashboard is a later experimental version than the one committed in the repo.

## Kubernetes Model

The project defines a custom resource:

- `ServiceSLO` in API group `autoscaling.fyp.io`

Each SLO includes:

- target deployment
- latency target
- min replicas
- max replicas
- priority (`primary` or `secondary`)

This gives the controller a service-specific scaling envelope and a distinction between the root path SLO and secondary services.

## Main Evaluation Story In The Repo

The repository is organized more like an experimental research artifact than a product codebase. The strongest evidence is in the scripts and `results/` tree.

### 1. Functional and metric validation

These runs check whether:

- the graph links match expected paths
- per-service metrics are produced for chosen routes
- baseline and low-rate path observations are credible

This is the validation layer for "can the system observe what it claims to observe?"

### 2. Bottleneck identification

These runs test whether the system can classify why latency is happening, for example:

- local CPU pressure
- downstream delay
- external or unmonitored delay
- ambiguous/non-CPU cases

This is the validation layer for "can the system explain the bottleneck?"

### 3. Bottleneck service targeting

These runs test whether the system can identify which service should be considered the bottleneck under chain or fanout conditions.

This is the validation layer for "can the system locate the right service to act on?"

### 4. Control-loop behavior

These runs evaluate how the scaling logic behaves under:

- short spikes
- sustained increases
- bursty workloads
- downstream bottlenecks

This is the validation layer for "does the controller react sensibly over time?"

### 5. Final benchmark

The final benchmark is frozen around Sock Shop `GET /login`, comparing:

- `HPA-50`
- `HPA-75`
- `ThriveScale`

The comparison emphasizes:

- SLO violation time
- SLO violation rate
- time to first protective action
- recovery behavior
- efficiency via replicas and requested resources

This is the validation layer for "is ThriveScale better than baseline autoscaling under the chosen benchmark?"

## Live Cluster Observations

The inspected server contains three important namespaces:

- `thrive-scale`: the autoscaling stack
- `sock-shop`: the primary evaluation application
- `thrive-demo`: the synthetic workload testbed

### What matched the repo

- the overall architecture is consistent with the repository design
- Sock Shop is the main target namespace
- the custom `ServiceSLO` CRD exists and is actively used
- the agent, aggregator, controller, frontend, and Redis are all deployed

### What differs from the repo

The live cluster is not a clean application of the checked-in YAML files. It includes:

- newer image tags than those in `deploy/`
- extra environment variables not present in the baseline manifests
- ConfigMap-based controller override
- ConfigMap-based frontend dashboard and nginx overrides

This means the live environment is best treated as an evolved experimental state, not as the canonical baseline in version control.

### Runtime signals observed

- the aggregator is successfully ingesting events from `1/1` agent pod
- the controller is running and repeatedly deciding `no_scale` under low traffic
- the aggregator reports live metrics for services such as `front-end`, `catalogue`, and `user`
- the inferred topology was sparse during inspection, suggesting either intentionally filtered observation or route-limited current traffic

### Important caution

The `sock-shop/shipping` deployment was in `CrashLoopBackOff` during inspection. This matters because:

- some routes may still function
- some full application paths may be degraded
- any benchmark or end-to-end claim collected from that cluster must be interpreted with care unless that state is intentional

## Thesis Versus Current Implementation

### Strong alignment

The repo clearly demonstrates:

- eBPF-based kernel-near observation
- service-aware latency attribution attempts
- topology/dependency reasoning
- SLO-driven scaling structure
- an emphasis on explainability via traces and explicit decision logic

### Partial alignment

The thesis emphasizes deterministic planning via queueing-network-style optimization. The current implementation instead appears to use:

- threshold logic
- streak and cooldown logic
- graph traversal
- local-vs-downstream attribution rules
- replica patching with bounded steps

That is still deterministic and explainable, but it is not obviously the same as a formal queueing-theory optimizer.

### Likely interpretation

For research writing, the safest interpretation is:

- the thesis describes the intended or conceptual ThriveScale approach
- the repository implements a substantial prototype of the sensing, attribution, and control framework
- the current runtime controller is heuristic and rule-based, even if inspired by deterministic planning ideas

## Research Strengths

- strong integration of observability and control
- clear attempt to go beyond CPU-only autoscaling
- real deployment target with Sock Shop
- experimental structure is organized and reproducible in spirit
- results folders preserve evidence artifacts, not only summary text
- live system includes explanation-oriented traces, which is valuable for research defensibility

## Research Gaps Or Questions

These are the main questions worth tracking in further research:

### 1. Formality of the scaling model

Is the autoscaler meant to be presented as:

- a heuristic causal autoscaler, or
- a queueing-theory-driven optimizer?

The current implementation supports the first more strongly than the second.

### 2. Ground truth and validation rigor

How are service-level latency attribution claims validated against known truth, especially for:

- asynchronous edges
- partial traffic paths
- DNS and infrastructure dependencies
- background kernel noise

### 3. Topology completeness

The inferred live topology during inspection was limited. That raises questions about:

- how complete the graph becomes under realistic load
- whether some dependencies are intentionally filtered
- how stable graph inference is across routes and workloads

### 4. Run-queue and throttling interpretation

Run-queue delay and throttling are interesting low-level signals, but their mapping to actionable bottlenecks still needs careful methodological framing to avoid overclaiming.

### 5. Experimental drift

The live cluster has drifted from the checked-in manifests. For thesis reproducibility, it would help to define:

- which repo revision is the canonical artifact
- which live settings were used for final results
- which overrides changed benchmark behavior

## Suggested Framing For Your Research Notes

If you want a concise description of this work, a safe wording is:

> ThriveScale is a prototype SLO-oriented autoscaling framework for Kubernetes that uses eBPF-derived runtime signals and dependency-aware service reasoning to support explainable scaling decisions with lower observability overhead than mesh-heavy approaches.

If you want a slightly more critical and precise wording:

> The current implementation demonstrates an eBPF-based, service-aware, deterministic control framework with heuristic bottleneck attribution and replica control. It aligns with the thesis goal of explainable SLO-driven autoscaling, while the formal queueing-theory optimization layer appears to be only partially realized in the checked-in runtime code.

## Best Files To Revisit Later

- `src/agent/agent.py`
- `src/agent/sensor.bt`
- `src/aggregator/aggregator.py`
- `src/controller/controller.py`
- `docs/evaluation/final-benchmark-plan.md`
- `docs/evaluation/sock-shop-deploy-and-paths.md`
- `results/README.md`

## Final Note

No code or cluster changes were made while preparing this brief. The server inspection was read-only.
