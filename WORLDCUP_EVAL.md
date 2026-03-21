# World Cup Evaluation Plan

This repo includes a paper-style evaluation path for comparing `ThriveScale` and `HPA` on Sock Shop using a scaled World Cup-like workload trace.

## Goal

Mirror the common literature pattern:

- replay a real bursty workload trace
- keep the testbed fixed
- compare autoscalers using:
  - SLO violation rate
  - exceedance severity
  - scaling actions
  - replica-seconds cost proxy

## Workflow

1. Prepare a scaled trace-derived workload profile.
2. Run the same replay against Sock Shop with `HPA`.
3. Run the same replay against Sock Shop with `ThriveScale`.
4. Analyze `violation vs cost`.

## 1. Prepare A World Cup Replay Profile

Input trace expectations:

- one numeric value per line, or
- CSV containing a numeric request column such as `requests`, `count`, `reqs`, `hits`, `rate`, or `rps`

Generate a replay profile:

```bash
python3 scripts/worldcup_prepare.py \
  --input /path/to/worldcup-trace.csv \
  --output deploy/03-evaluation/workloads/worldcup-sockshop.yaml \
  --source-interval-seconds 60 \
  --bucket-seconds 60 \
  --target-peak-rps 180
```

Notes:

- `--source-interval-seconds` is used when the input values are request counts rather than RPS.
- `--target-peak-rps` scales the trace to fit your cluster capacity while preserving burst shape.
- The generated YAML includes a Sock Shop route mix suitable for `front-end`.

If you do not yet have a real trace file, the repo includes:

- [deploy/03-evaluation/workloads/worldcup-sockshop-template.yaml](/d:/Selve/Documents/IIT/YEAR%204/FYP/Code/new-arch-setup/deploy/03-evaluation/workloads/worldcup-sockshop-template.yaml)

This is a starter profile only, not a real World Cup replay.

## 2. Run The Comparison

Use the prepared mix file with the dedicated runner:

```bash
MIX_FILE=deploy/03-evaluation/workloads/worldcup-sockshop.yaml \
make compare-worldcup
```

If you want to use the starter file:

```bash
make compare-worldcup
```

The runner:

- resets Sock Shop to a clean baseline before each run
- runs `HPA` first, then `ThriveScale`
- reuses the same workload profile for both
- writes results under `results/worldcup-compare/`

## 3. Outputs

Main outputs:

- per-run CSVs in `results/worldcup-compare/`
- summary markdown in `results/analysis/worldcup_thrive_vs_hpa.md`
- plots in `results/analysis/`

Metrics reported:

- SLO adherence
- SLO violation rate
- mean exceedance
- p95 exceedance
- sampled and system replica-seconds
- scaling actions

## Important Note

For a fair thesis comparison, Sock Shop must provide meaningful QoS latency truth to the aggregator. If `p90_latency` and `truth_p90_latency_ms` remain zero during replay, the comparison is still useful for load-path validation but not yet a final thesis-grade autoscaling result.
