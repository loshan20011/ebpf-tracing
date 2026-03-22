# Control-Loop Tests

## Purpose
This phase evaluates how ThriveScale understands changing traffic over time, reevaluates conditions across repeated control windows, determines replica counts, and protects the SLO without unnecessary oscillation.

The emphasis is not only "did scaling happen". The emphasis is:

- traffic pattern understanding
- repeated short-window and sustain-window reevaluation
- replica count determination over time
- SLO protection with minimal thrashing

## Window Model
- Control loop: `10s`
- Short classification window: `20s`
- Sustain / action window: `60s`
- Downscale window: `90s`

Interpretation:

- the `20s` window should make the controller responsive to genuine pressure
- the `60s` window should keep replica sizing from reacting to one noisy burst
- the `90s` window should delay downscale until recovery is clearly sustained

## Cases
| Case | Pattern | Main Expectation |
| --- | --- | --- |
| `CL1_short_spike` | brief spike followed by a quick drop | no aggressive overscaling, low oscillation, at most a small protective reaction |
| `CL2_sustained_increase` | traffic rises and stays high | progressive scale-up, stable final replica count, limited SLO violation duration |
| `CL3_rise_then_recovery` | traffic rises and later falls | scale-up on the rise, delayed safe downscale after recovery, no immediate scale-down after breach |
| `CL4_bursty_repeated_spikes` | repeated spike/calm alternation | avoid thrashing, let windows smooth behavior, keep SLO mostly protected |
| `CL5_downstream_sustained_bottleneck` | sustained pressure with a true downstream bottleneck | keep targeting the real bottleneck service instead of repeatedly scaling the parent/root |

## Per-Case Expectations
### `CL1_short_spike`
- Expected traffic pattern: short warmup, one brief spike, then quick recovery
- Expected controller behavior: at most one small protective or cautious upscale, then hold
- Expected replica trend: stay near baseline and avoid aggressive overscaling
- Expected SLO behavior: brief degradation is acceptable, prolonged SLO breach is not
- Pass/fail criteria: low action count, low oscillation, short total time above SLO

### `CL2_sustained_increase`
- Expected traffic pattern: low load, rising load, then sustained high load
- Expected controller behavior: progressive scale-up under repeated reevaluation
- Expected replica trend: climb in steps, then settle at a stable higher count
- Expected SLO behavior: some early breach is acceptable, but SLO should recover after scaling catches up
- Pass/fail criteria: at least one upscale, stable final replicas, bounded breach duration

### `CL3_rise_then_recovery`
- Expected traffic pattern: rise, hold, then extended recovery
- Expected controller behavior: scale during pressure, delay downscale until calm demand is sustained
- Expected replica trend: increase first, hold through early recovery, then downscale later
- Expected SLO behavior: recovery should happen before downscale begins
- Pass/fail criteria: both upscale and delayed downscale occur, with low oscillation

### `CL4_bursty_repeated_spikes`
- Expected traffic pattern: repeated spike/calm alternation, then settle
- Expected controller behavior: windows smooth behavior so the controller does not chase every burst
- Expected replica trend: limited adjustment is acceptable, repeated flapping is not
- Expected SLO behavior: mostly protected with only short breach intervals
- Pass/fail criteria: bounded action count, low oscillation, mostly protected SLO

### `CL5_downstream_sustained_bottleneck`
- Expected traffic pattern: sustained downstream bottleneck behind the entry path
- Expected controller behavior: repeatedly target the true downstream bottleneck service
- Expected replica trend: downstream bottleneck replicas rise while root and parent avoid repeated incorrect scaling
- Expected SLO behavior: gateway SLO should improve as downstream capacity increases
- Pass/fail criteria: target selection remains focused on the downstream bottleneck, final replicas stabilize, oscillation remains low

## Collected Outputs
For each case the phase stores:

- client p90 over time
- service p90 over time
- truth p90 over time when available
- p90 latency over time
- SLO violation count
- total time above SLO
- time to first action
- time to recovery below SLO
- replica count over time
- scale action count
- oscillation count
- peak replicas
- under-provisioned duration
- over-provisioned duration when it can be inferred

## Result Location
- guide: `docs/evaluation/control-loop-tests.md`
- per-case results: `results/nonfunctional/control_loop/<case-name>/`
- phase table: `results/nonfunctional/control_loop/phase_summary.md`

Each case directory contains:

- `request_log.ndjson`
- `aggregator_graph.ndjson`
- `controller_traces.ndjson`
- `control_state.ndjson`
- `replica_counts.ndjson`
- `timeseries.json`
- `summary.json`
- `summary.md`

## Run
All control-loop cases:

```bash
bash src/scripts/evaluation/run_control_loop_phases.sh all
cat results/nonfunctional/control_loop/phase_summary.md
```

Single case:

```bash
python3 src/scripts/evaluation/run_control_loop_case.py \
  --case-config src/scripts/evaluation/control_loop/cases/CL2_sustained_increase.json \
  --output-root results/nonfunctional/control_loop \
  --mode control
```

## Reading The Results
The summary for each case is intended to answer four questions:

1. Did the controller interpret the traffic pattern reasonably?
2. Did repeated reevaluation lead to sensible replica counts over time?
3. Did the system keep the SLO violation window limited?
4. Did the controller stay stable instead of oscillating on every short fluctuation?

This phase should be used before Sock Shop scenario testing or HPA benchmarking, because it validates the controller's time-based scaling behavior directly.

Interpretation should stay aligned with current controller logic:

- graph plus latency and dependency evidence finds the bottleneck path and service
- runq p90 is only a CPU-scaleability support signal at the final bottleneck service
- root or gateway high runq alone is not proof of a true local bottleneck
- primary mode may still allow a small protective root action during sudden ingress spikes
- secondary mode should remain conservative
