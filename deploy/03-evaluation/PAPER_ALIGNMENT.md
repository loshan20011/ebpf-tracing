# Paper Alignment Notes

## Explicit References Found In The Papers

### STaleX

- workload generator: `LOCUST.io`
- workload source: `WorldCup98`
- bibliography entry: `M. Arlitt and T. Jin, "A workload characterization study of the 1998 World Cup Web site", IEEE Network, 2000`
- application path under study: `/login`
- compared services: `front-end`, `user`, and `carts`
- SLO: `200 ms`
- violation reporting style:
  - summed response-time exceedance in `ms`
  - effectively an aggregate violation magnitude rather than a rate
- HPA threshold sets reported in the paper:
  - uniform: `25 / 25 / 25`
  - uniform: `50 / 50 / 50`
  - uniform: `60 / 60 / 60`
  - uniform: `70 / 70 / 70`
  - differentiated: `50 / 50 / 25`
  - differentiated: `70 / 70 / 40`
  - differentiated: `70 / 80 / 50`
  - differentiated: `40 / 60 / 70`

### Key Considerations For Auto-Scaling

- workload generator: `LOCUST.io`
- workload source: `WorldCup98`
- bibliography entry: `M. Arlitt and T. Jin, "A workload characterization study of the 1998 World Cup Web site", IEEE Network, 2000`
- studied chain: Sock Shop `/login` path
- evaluation emphasis: full service chain, probes, observability, fixed requests/limits, and fair replay under the same workload

### PBScaler

- violation reporting style:
  - `SLO violation rate (%)`
  - explicitly defined using end-to-end `P90` tail latency against the SLO
- also reports:
  - response time distribution
  - resource cost

## What Is Exact In This Repo

- the environment is Sock Shop on Kubernetes
- the compared HPA threshold sets match the STaleX paper values
- the SLO target is set to `200 ms`
- the raw WorldCup98 source used by this repo comes from the original LBL trace host referenced by `scripts/worldcup98_trace_extract.py`
  - source host: `https://ita.ee.lbl.gov/traces/WorldCup`

## What Is Still A Controlled Deviation

- the current repo replay uses a WorldCup98-derived time series mapped onto Sock Shop routes
- the current harness is not the exact LOCUST login user-journey implementation from the paper
- because of that, this repo reproduces the paper's:
  - trace family
  - service set
  - HPA threshold sweep
  - SLO
  - environment discipline
  but not the paper's exact closed-source Locust scenario

## Final Evaluation Positioning

Use this repo's final evaluation as:

- `paper-aligned` for methodology and parameterization
- `not byte-for-byte identical` to the original Locust user script unless a dedicated login-flow load generator is added later
- reported with both:
  - PBScaler-style `violation_rate (%)`
  - STaleX-style `violation_magnitude_ms`
