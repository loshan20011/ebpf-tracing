Benchmark scripts:

- `run_final_login_benchmark.sh`: final Sock Shop benchmark runner for `noautoscale`, `hpa50`, `hpa70`, and `thrivescale`
- `run_simple_login_benchmark.py`: execute one fixed `/login` benchmark arm and save per-window client and replica metrics
- `seed_login_users.py`: create reusable `/login` users through `/register`
- `plot_benchmark_curves.py`: build SVG comparison graphs from completed benchmark result folders
- `legacy/calibrate_login_rps.py`: legacy helper, kept only for reference and not used by the frozen final benchmark

Final benchmark flow:

```bash
bash src/scripts/benchmark/run_final_login_benchmark.sh prepare
bash src/scripts/benchmark/run_final_login_benchmark.sh run-arm noautoscale
bash src/scripts/benchmark/run_final_login_benchmark.sh run-arm hpa50
bash src/scripts/benchmark/run_final_login_benchmark.sh run-arm hpa70
bash src/scripts/benchmark/run_final_login_benchmark.sh run-arm thrivescale
bash src/scripts/benchmark/run_final_login_benchmark.sh compare
python3 src/scripts/benchmark/plot_benchmark_curves.py --results-root results/benchmark/login
```

Final result folders:

- `results/benchmark/login/noautoscale`
- `results/benchmark/login/hpa50`
- `results/benchmark/login/hpa70`
- `results/benchmark/login/thrivescale`
- `results/benchmark/login/violation_rate_vs_rps.svg`
- `results/benchmark/login/replica_delta_vs_rps.svg`

Keep any future benchmark outputs inside those arm folders instead of creating ad-hoc folders under `results/benchmark`.

Carts validity:

- each arm also saves `carts_state_before.json`, `carts_state_after.json`, and `carts_failure_visibility.json`
- if `carts` restarts during the run, the arm is marked invalid
- if `carts` shows hidden failure patterns while front-end success still looks normal, the arm is marked invalid

Frozen benchmark resources:

- `front-end`: `100m/500m`, `128Mi/256Mi`
- `user`: `100m/300m`, `128Mi/256Mi`
- `carts`: `300m/600m`, `512Mi/1Gi`

Frozen benchmark bounds:

- `front-end`: `1-10`
- `user`: `1-13`
- `carts`: `1-13`

Frozen SLOs:

- client benchmark SLO: `p90 < 150 ms`
- ThriveScale `front-end`: `150 ms`
- ThriveScale `user`: `100 ms`
- ThriveScale `carts`: `100 ms`
