# ThriveScale

ThriveScale is a deterministic, dependency-aware horizontal autoscaling prototype for Kubernetes. It uses eBPF-informed runtime signals, service-level latency, and observed service dependencies to decide which deployment should scale.

## Layout

Canonical runtime code:

- `src/agent`
- `src/aggregator`
- `src/controller`
- `src/frontend`
- `src/load-generator`
- `src/test-workloads`

Support paths:

- `deploy/` for Kubernetes manifests
- `scripts/` for helper and evaluation scripts
- `tests/` for lightweight automated checks
- `results/` for generated benchmark and validation outputs

## Core Runtime

- `src/agent`: node-local collector using `bpftrace`
- `src/aggregator`: graph and metric synthesis service
- `src/controller`: minimal SLO-driven autoscaler
- `deploy/00-setup/crd-definition.yaml`: `ServiceSLO` CRD

The default target is the Chapter 1 scope:

- deterministic
- dependency-aware
- horizontal only
- minimal stable deployment first
- QoS-led decisions that only scale when kernel-observed CPU pressure suggests the bottleneck is local rather than just downstream waiting

## Build And Deploy On EC2

Run directly on the EC2 K3s host:

```bash
make build
make load
make deploy
make validate-runtime
```

For a clean external-app deployment path on the EC2 host:

```bash
make wipe-k3s-demo
make build-system
make load-system
make deploy-thrivescale
make deploy-sockshop-demo
make deploy-sockshop-slos
make status
```

Remote access policy:

- use short `ssh` checks only
- do not use `scp` as part of the default workflow
- do file updates directly on the EC2 host when remote SSH reliability is a concern
- split remote work into small steps instead of one long session

See `EC2_WORKFLOW.md` for the standard operating pattern.
See `SETUP.md` for the full deployment and verification flow.

Default namespaces:

- control plane: `thrive-scale`
- app namespace: `sock-shop`

Default demo assets:

- workloads: `deploy/02-demo-apps/workloads.yaml`
- slos: `deploy/02-demo-apps/my-slos.yaml`
- controller mode: simple three-outcome controller

Benchmark-only assets:

- evaluation SLOs and manifests: `deploy/03-evaluation/`
- analysis helper: `scripts/analyze_eval.py`
- demo comparison runner: `scripts/run_demo_compare.sh`
- World Cup trace prep: `scripts/worldcup_prepare.py`
- World Cup comparison runner: `scripts/run_worldcup_compare.sh`
- Sock Shop route profiler: `scripts/profile_sockshop_routes.py`
- evaluation scope and scenarios: `THESIS_SCOPE.md`

## ThriveScale vs HPA Demo Comparison

For the current synthetic thesis demo, use:

```bash
make compare-demo
```

This comparison path:

- verifies a clean baseline before each phase
- runs the same gateway workload against HPA and ThriveScale
- records client latency, per-service p90 snapshots, and cluster-wide replica totals
- summarizes SLO violation rate and replica-seconds cost proxy with `scripts/analyze_eval.py`

Key comparison assets:

- HPA baseline: `deploy/03-evaluation/hpa-demo.yaml`
- workload mix: `deploy/03-evaluation/workloads/demo-thrivescale-vs-hpa.yaml`
- results: `results/demo-compare/`

## World Cup-Style Sock Shop Comparison

To follow the paper-style `SLO violation vs cost` pattern with a bursty trace replay:

```bash
python3 scripts/worldcup_prepare.py \
  --input /path/to/worldcup-trace.csv \
  --output deploy/03-evaluation/workloads/worldcup-sockshop.yaml \
  --source-interval-seconds 60 \
  --bucket-seconds 60 \
  --target-peak-rps 180

MIX_FILE=deploy/03-evaluation/workloads/worldcup-sockshop.yaml \
make compare-worldcup
```

If you only want the starter profile before the real dataset is prepared:

```bash
make compare-worldcup
```

See `WORLDCUP_EVAL.md` for the full workflow and cautions about QoS truth collection on Sock Shop.

## Sock Shop Route Profiling

Before the final World Cup comparison, profile the Sock Shop routes and pick the path that produces the strongest local CPU-bound behavior:

```bash
python3 scripts/profile_sockshop_routes.py \
  --freeze-thrivescale \
  --delete-hpa \
  --reset-aggregator
```

Or:

```bash
make profile-sockshop-routes
```

This step ranks the routes by route-level latency rise, target-service run queue pressure, and local handling dominance. See `SOCKSHOP_ROUTE_PROFILING.md` for the interpretation guide.

## Local Validation

```bash
make validate
```

## Sock Shop Demo

The external Sock Shop demo is deployed through:

- `scripts/deploy_sock_shop_demo.sh`
- `make deploy-sockshop-demo`
- `make deploy-sockshop-slos`

The deploy script keeps the external repo out of this workspace, clones it on the Linux host, applies the K3s compatibility fixes we validated, and then deploys it into `sock-shop`.

## Notes

- Images are built locally and imported into K3s.
- Docker Hub push is not part of the default workflow.
- The frontend is part of the normal ThriveScale deployment path.
- Legacy benchmark assets are kept out of the default deploy path.
- The safe EC2 workflow avoids `scp` and long chained remote sessions.
