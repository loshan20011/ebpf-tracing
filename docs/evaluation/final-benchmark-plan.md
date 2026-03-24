# Final Sock Shop Benchmark Plan

Application: `Sock Shop`

Frozen benchmark:
- path: `GET /login`
- auth: `Authorization: Basic <base64(username:password)>`
- user pool: seeded through `/register` only
- client-side SLO: `10s p90 < 150 ms`
- benchmark arms: `HPA-60`, `HPA-80`, `ThriveScale`
- one final run per arm only

Frozen resources:
- `front-end`: request `100m / 128Mi`, limit `500m / 256Mi`
- `user`: request `100m / 128Mi`, limit `300m / 256Mi`
- `carts`: request `300m / 512Mi`, limit `600m / 1Gi`

Frozen replica bounds:
- `front-end`: min `1`, max `10`
- `user`: min `1`, max `13`
- `carts`: min `1`, max `13`

Frozen ThriveScale service SLOs:
- `front-end`: `150 ms`
- `user`: `100 ms`
- `carts`: `100 ms`

Frozen 15-minute workload:
- `2 min @ 100 RPS`
- `3 min @ 150 RPS`
- `4 min @ 250 RPS`
- `3 min @ 175 RPS`
- `3 min @ 325 RPS`

Fallback rule:
- if the first attempted arm shows immediate collapse before benchmarking is meaningful, reduce all phase RPS values once by the same factor
- freeze that revised workload and reuse it for all three arms

Benchmark truth:
- use client-side end-to-end 10-second windows
- use client-side p90 as the primary SLO source of truth

Primary comparison metrics:
- SLO violation time
- SLO violation rate
- time to first protective action
- recovery time
- error rate

Efficiency metrics:
- peak replicas
- average replicas
- requested CPU core-minutes
- replica-seconds

Saved outputs per arm:
- exact workload profile
- exact SLO
- exact pod resources
- exact replica bounds
- per-window client p90
- per-window error rate
- per-window replica counts
- per-window violation marker
- Carts failure visibility bundle
- invalid-run flag if Carts restarts or hidden Carts failure is detected
- summary JSON
- arm summary markdown

Canonical commands:

```bash
bash src/scripts/benchmark/run_final_login_benchmark.sh prepare
bash src/scripts/benchmark/run_final_login_benchmark.sh run-arm hpa60
bash src/scripts/benchmark/run_final_login_benchmark.sh run-arm hpa80
bash src/scripts/benchmark/run_final_login_benchmark.sh run-arm thrivescale
bash src/scripts/benchmark/run_final_login_benchmark.sh compare
```
