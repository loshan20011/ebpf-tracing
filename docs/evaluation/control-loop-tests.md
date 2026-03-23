# Control-Loop Tests

## Purpose
This phase evaluates how ThriveScale understands changing traffic over time, reevaluates conditions across repeated control windows, determines replica counts, and protects the SLO without unnecessary oscillation.

Current control-loop validation is now centered on the real Sock Shop application in the `sock-shop` namespace rather than the synthetic `thrive-demo` routes. The goal is to validate controller behavior on application traffic patterns where scaling may or may not improve the user-facing path.

The emphasis is not only "did scaling happen". The emphasis is:

- traffic pattern understanding
- repeated short-window and sustain-window reevaluation
- replica count determination over time
- SLO protection with minimal thrashing

For the current Sock Shop phase, the request generator uses intentionally stronger catalogue/cart traffic than the earlier demo-based runs. The working load tiers are now centered around roughly `100`, `250`, `300`, and `450` request-per-second targets in the pressure phases so the controller sees meaningful application pressure.

## Window Model
- Control loop: `10s`
- Short classification window: `20s`
- Sustain / action window: `60s`
- Downscale window: `90s`
- Control-loop replica ceiling for these cases: `maxReplicas=8` during this phase only

Interpretation:

- the `20s` window should make the controller responsive to genuine pressure
- the `60s` window should keep replica sizing from reacting to one noisy burst
- the `90s` window should delay downscale until recovery is clearly sustained

## Cases
| Case | Pattern | Main Expectation |
| --- | --- | --- |
| `CL1_short_spike` | brief `GET /catalogue` spike followed by a quick drop | no aggressive overscaling, low oscillation, at most a small protective reaction |
| `CL2_sustained_increase` | `GET /catalogue` rises and stays high | progressive scale-up, stable final replica count, visible improvement if capacity helps |
| `CL3_rise_then_recovery` | `GET /catalogue` rises and later falls | scale-up on the rise, delayed safe downscale after recovery, no immediate scale-down after breach |
| `CL4_bursty_repeated_spikes` | repeated `GET /catalogue` spike/calm alternation | avoid thrashing, let windows smooth behavior, keep SLO mostly protected |
| `CL5_downstream_sustained_bottleneck` | sustained `GET /cart` pressure on a downstream cart path | keep targeting the real cart-path bottleneck instead of repeatedly scaling only the root |

## Per-Case Expectations
### `CL1_short_spike`
- Expected traffic pattern: short `GET /catalogue` warmup, one brief spike, then quick recovery
- Expected controller behavior: at most a small protective or cautious upscale, then hold
- Expected replica trend: stay near baseline and avoid aggressive overscaling
- Expected SLO behavior: brief front-end degradation is acceptable, prolonged SLO breach is not
- Pass/fail criteria: low action count, low oscillation, short total time above SLO

### `CL2_sustained_increase`
- Expected traffic pattern: low `GET /catalogue` load, rising load, then sustained high load
- Expected controller behavior: progressive scale-up under repeated reevaluation on the service path the controller identifies
- Expected replica trend: climb in steps, then settle at a stable higher count
- Expected SLO behavior: some early breach is acceptable, but scaling should improve pressure over time if the path is actually scalable
- Pass/fail criteria: at least one upscale, stable final replicas, bounded breach duration, and visible improvement when capacity helps

### `CL3_rise_then_recovery`
- Expected traffic pattern: `GET /catalogue` rise, hold, then extended recovery
- Expected controller behavior: scale during pressure, delay downscale until calm demand is sustained
- Expected replica trend: increase first, hold through early recovery, then downscale later
- Expected SLO behavior: front-end recovery should happen before downscale begins
- Pass/fail criteria: both upscale and delayed downscale occur, with low oscillation

### `CL4_bursty_repeated_spikes`
- Expected traffic pattern: repeated `GET /catalogue` spike/calm alternation, then settle
- Expected controller behavior: windows smooth behavior so the controller does not chase every burst
- Expected replica trend: limited adjustment is acceptable, repeated flapping is not
- Expected SLO behavior: mostly protected with only short front-end breach intervals
- Pass/fail criteria: bounded action count, low oscillation, mostly protected SLO

### `CL5_downstream_sustained_bottleneck`
- Expected traffic pattern: sustained `GET /cart` pressure on a downstream cart path
- Expected controller behavior: repeatedly target the true downstream cart-path bottleneck service rather than only the `front-end`
- Expected replica trend: downstream cart-path replicas rise while root-only scaling stays limited
- Expected SLO behavior: front-end SLO should improve as downstream cart capacity increases
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
- controller scale proposals over time
- actual replica changes from replica observations
- oscillation count
- peak replicas
- capacity ceiling reached
- recovered below SLO
- under-provisioned duration
- over-provisioned duration when it can be inferred

## Result Location
- guide: `docs/evaluation/control-loop-tests.md`
- per-case results: `results/replica_count/control_loop/<case-name>/`
- phase table: `results/replica_count/control_loop/phase_summary.md`

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
bash src/scripts/control_loop/run_control_loop_phases.sh all
cat results/replica_count/control_loop/phase_summary.md
```

Single case:

```bash
python3 src/scripts/control_loop/run_control_loop_case.py \
  --case-config src/scripts/control_loop/cases/CL2_sustained_increase.json \
  --output-root results/replica_count/control_loop \
  --mode control
```

## Reading The Results
The summary for each case is intended to answer four questions:

1. Did the controller interpret the traffic pattern reasonably?
2. Did repeated reevaluation lead to sensible replica counts over time?
3. Did the system keep the SLO violation window limited?
4. Did the controller stay stable instead of oscillating on every short fluctuation?

This phase now acts as the first application-based control-loop check on Sock Shop before broader scenario testing or HPA benchmarking.

Interpretation should stay aligned with current controller logic:

- graph plus latency and dependency evidence finds the bottleneck path and service
- final control reasoning uses:
  - `downstream_delay`
  - `external_or_unmonitored_delay`
  - `local_bottleneck`
- `local_bottleneck` means the current service is the main local slow point and becomes the bounded trial scale candidate
- runq p90 and CPU throttling are supporting indicators only; they are not hard gates for trial scaling
- root or gateway high runq alone is not proof of a true local bottleneck
- primary mode may still allow a small protective root action during sudden ingress spikes
- secondary mode should remain conservative
- for the current control-loop runs, upscale cooldown is set to `0s` so we can observe raw controller responsiveness more directly
- downscale cooldown remains delayed so recovery is not followed by immediate flap-down
