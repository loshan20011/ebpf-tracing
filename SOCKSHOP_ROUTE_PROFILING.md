# Sock Shop Route Profiling

Use this step before the final HPA vs ThriveScale comparison when you want to identify which Sock Shop paths create the strongest CPU-bound behavior.

The goal is not to guess a hot path. The goal is to replay one route at a time, ramp the load in steps, and rank the routes by:

- user-facing route latency rise
- target-service run queue pressure
- target-service local handling dominance over downstream wait
- low instability from bad routes such as 404 or 401 paths

## What The Script Does

`scripts/profile_sockshop_routes.py`:

- reads the live Sock Shop benchmark metadata from the aggregator
- builds one-route workload mixes
- replays stepped load against each route
- samples `/api/graph` during each run
- ranks routes by a `cpu_bound_score`

The script uses:

- route-level truth latency from the `front-end`
- target-service `avg_runq_latency`
- target-service `exclusive_delay`
- target-service `p90_latency`
- target-service `rps`

This is the right pre-step for a CPU-focused ThriveScale evaluation, because it tells us which path actually creates local CPU/scheduler pressure instead of mostly downstream waiting.

## Recommended Run

Run this on the EC2 K3s host from a clean Sock Shop baseline:

```bash
python3 scripts/profile_sockshop_routes.py \
  --freeze-thrivescale \
  --delete-hpa \
  --reset-aggregator
```

Or use:

```bash
make profile-sockshop-routes
```

Note:

- `make profile-sockshop-routes` freezes ThriveScale and resets the aggregator, but it does not delete HPAs automatically.
- If HPAs are still present, the script will stop and tell you.

## Default Route Set

By default the script uses the safe Sock Shop route catalog already used in the World Cup replay:

- `/`
- `/catalogue`
- `/category.html`
- `/detail.html?id=...`
- `/basket.html`
- `/customer-orders.html`

This avoids known weak routes such as anonymous paths that returned `404` or `401` in earlier tests.

## Output

Each run writes artifacts under:

```text
results/route-profiles/<timestamp>/
```

Key files:

- `route_summary.csv`
- `route_summary.json`
- `route_summary.md`
- `mix_<route>.yaml`
- `harness_<route>.csv`
- `graph_<route>.csv`
- `harness_<route>.log`

## How To Read The Result

Routes near the top are the best candidates for the final CPU-bound benchmark.

The best routes usually have:

- `peak_route_p90_ms` high enough to matter for QoS
- `peak_target_runq_ms` above about `6 ms`
- `peak_target_local_share` high, meaning local handling dominates
- low `peak_route_error_rate`

Use the top `strong_cpu_bound` route, or the top `mixed_cpu_bound` route if no route is classified as `strong`.

## Suggested Thesis Use

1. Profile the routes first.
2. Pick the highest-ranked CPU-bound path.
3. Build the final World Cup replay around that path or a route mix dominated by that path.
4. Run HPA and ThriveScale on the exact same replay.

That makes the evaluation much fairer for a CPU-focused autoscaler.
