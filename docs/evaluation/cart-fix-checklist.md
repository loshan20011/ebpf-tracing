# Carts Fix Checklist For Final /login Benchmark

This checklist is the benchmark-readiness gate for Sock Shop `carts` before running the final `/login` comparison arms.

## Fixed Benchmark Constraints

- Path: `GET /login`
- Auth: Basic auth
- Seed users only through `/register`
- Benchmark services in scope: `front-end`, `user`, `carts`
- Client SLO truth: end-to-end client-side `p90 < 150 ms`
- Frozen `carts` benchmark resources:
  - CPU request `300m`
  - CPU limit `600m`
  - memory request `512Mi`
  - memory limit `1Gi`
- Frozen `carts` bounds:
  - min replicas `1`
  - max replicas `13`

## Carts Readiness Requirements

- `carts` must expose a working app health endpoint at `/health`
- readiness must not mark `carts` healthy before JVM/app startup is complete
- liveness must not restart `carts` during slow but valid startup
- startup protection must prevent premature liveness failure
- benchmark collection must start only after:
  - all required pods are `Ready`
  - manual `/login` succeeds
  - `carts` health check succeeds
  - `60-90s` idle stabilization completes
  - `carts` shows no restart increase during idle stabilization

## Hidden Failure Validity Checks

The final benchmark must not trust front-end `200` responses alone.

For every arm, save and inspect:

- `carts_state_before.json`
- `carts_state_after.json`
- `carts_failure_visibility.json`
- `summary.json`

Treat the run as invalid if either happens:

- `carts` restart count increases during the benchmark
- `carts` shows failure patterns while front-end success still looks normal

Current hidden failure rule in the runner:

- if `carts` restart delta is greater than `0`, mark run invalid
- if `carts` error-pattern logs appear and front-end success rate is at least `95%`, mark run invalid as hidden cart failure

## Required Observability

The benchmark bundle must retain:

- per-window client `p90`
- per-window client error rate
- per-window replica counts
- `carts` restart delta
- `carts` failure visibility log summary
- final validity flag for the arm

## Ready-To-Run Gate

Do not start an arm unless all pass:

1. `/login` returns `200` for a seeded `/register` user
2. `front-end`, `user`, `carts`, `user-db`, `carts-db` are `Ready`
3. no required path pod is `Pending` or `CrashLoopBackOff`
4. `carts` health endpoint responds from inside the cluster
5. `carts` does not flap during idle stabilization
6. only one autoscaler is active for the selected arm
7. frozen resources and bounds are applied

If any check fails, fix Sock Shop first and rerun the gate.
