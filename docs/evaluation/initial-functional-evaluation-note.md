# Initial Functional Evaluation Note

## Purpose

This note records how the initial functional validation was carried out, what tools were used, how the results were interpreted, and how to capture terminal-friendly evidence for screenshots.

The goal of this stage was not large-scale performance benchmarking. It was to verify that:

- ThriveScale graph discovery matches real call paths
- ThriveScale latency and throughput metrics are directionally correct
- the platform can distinguish service-local behavior from downstream behavior
- the control-plane inputs are trustworthy enough to proceed to bottleneck-injection tests

## What We Tested

Two environments were used:

- Sock Shop
  - used first to validate route-path observation on a realistic microservice app
  - routes such as `/catalogue`, `/cart`, and `POST /cart` were used
- Demo workload namespace `thrive-demo`
  - used to validate the platform on simpler synthetic paths where service interactions are easier to reason about
  - routes tested:
    - `/cpu`
    - `/io`
    - `/chain`
    - `/fanout`

## Test Method

The initial functional validation was done with short, isolated route tests.

Common method:

- reset ThriveScale state before each run
- run one route only
- keep load low and stable
- disable autoscaling during observation-only checks
- run the load for 1 minute
- inspect the graph and service metrics immediately after the run

For the final clean demo runs, the practical settings were:

- duration: `1 minute`
- load tool: `hey`
- traffic shape: `-c 1 -q 10`
  - this gives about `10 rps total`

## Tools Used

Traffic generation:

- `hey`
  - used to generate steady HTTP traffic for a fixed duration

Platform metric capture:

- `curl`
  - used to reset the aggregator and fetch `/api/graph`
- `jq`
  - used to print the important graph and metric fields in a readable way

Cluster/runtime inspection:

- `kubectl`
  - used to check pod state, rollout status, and deployment health

## How We Analyzed Results

For each isolated route run, we compared:

- client-side latency from `hey`
- platform-reported p90 from ThriveScale
- platform-reported RPS from ThriveScale
- observed topology edges from `/api/graph`

What counted as a good result:

- the graph shows the expected path
- platform RPS is close to generated traffic
- platform latency is in the same general range as client latency
- unrelated services stay inactive or irrelevant

Important interpretation rule:

- the main p90 should come from ThriveScale’s own system-derived metric path
- app-published truth can still be used as validation support, but not as the required primary source in a real deployment

## Main Fixes Applied Before Finalizing Initial FR

The final clean functional result depended on these corrections:

- strict HTTP-method-based request start detection in the probe
- removal of overly broad request detection from `recvmsg`
- tighter service/IP attribution
- fixing the probe timing so reused outbound client sockets do not create fake cross-request latency
- making the platform prefer its own corroborated p90 signal before falling back to truth

These fixes were especially important for the `svc-chain` path, which initially showed an inflated latency until request timing was tightened.

## How To Attach Results

The easiest way is:

1. run the command in the terminal
2. wait until the metric output is printed
3. take a screenshot of:
   - the `hey` summary
   - the `/api/graph` metric output
4. attach that screenshot in the report

Recommended screenshot content:

- route used
- `hey` p90 and requests/sec
- topology section
- the relevant service metric blocks only

## Screenshot Commands

### Demo Namespace

These are the cleanest commands for terminal screenshots.

CPU case:

```bash
curl -fsS "http://127.0.0.1:30938/api/reset"
sleep 8
hey -z 1m -c 1 -q 10 "http://172.31.32.23:80/cpu?count=1500000"
curl -fsS "http://127.0.0.1:30938/api/graph" | jq '{
  topology,
  gateway: .metrics.gateway,
  svc_cpu: .metrics["svc-cpu"]
}'
```

IO case:

```bash
curl -fsS "http://127.0.0.1:30938/api/reset"
sleep 8
hey -z 1m -c 1 -q 10 "http://172.31.32.23:80/io"
curl -fsS "http://127.0.0.1:30938/api/graph" | jq '{
  topology,
  gateway: .metrics.gateway,
  svc_io: .metrics["svc-io"]
}'
```

Chain case:

```bash
curl -fsS "http://127.0.0.1:30938/api/reset"
sleep 8
hey -z 1m -c 1 -q 10 "http://172.31.32.23:80/chain?count=1500000"
curl -fsS "http://127.0.0.1:30938/api/graph" | jq '{
  topology,
  gateway: .metrics.gateway,
  svc_chain: .metrics["svc-chain"],
  svc_cpu: .metrics["svc-cpu"]
}'
```

Fanout case:

```bash
curl -fsS "http://127.0.0.1:30938/api/reset"
sleep 8
hey -z 1m -c 1 -q 10 "http://172.31.32.23:80/fanout?count=1500000"
curl -fsS "http://127.0.0.1:30938/api/graph" | jq '{
  topology,
  gateway: .metrics.gateway,
  svc_fanout: .metrics["svc-fanout"],
  svc_cpu: .metrics["svc-cpu"],
  svc_io: .metrics["svc-io"]
}'
```

### Sock Shop

Use these when ThriveScale is pointed back to `sock-shop`.

Catalogue route:

```bash
curl -fsS "http://127.0.0.1:30938/api/reset"
sleep 8
hey -z 1m -c 1 -q 10 "http://172.31.32.23:30001/catalogue"
curl -fsS "http://127.0.0.1:30938/api/graph" | jq '{
  topology,
  front_end: .metrics["front-end"],
  catalogue: .metrics["catalogue"]
}'
```

Cart route:

```bash
curl -fsS "http://127.0.0.1:30938/api/reset"
sleep 8
hey -z 1m -c 1 -q 10 "http://172.31.32.23:30001/cart"
curl -fsS "http://127.0.0.1:30938/api/graph" | jq '{
  topology,
  front_end: .metrics["front-end"],
  carts: .metrics["carts"]
}'
```

Customers/login-related route:

```bash
curl -fsS "http://127.0.0.1:30938/api/reset"
sleep 8
hey -z 1m -c 1 -q 10 "http://172.31.32.23:30001/customers"
curl -fsS "http://127.0.0.1:30938/api/graph" | jq '{
  topology,
  front_end: .metrics["front-end"],
  user: .metrics["user"]
}'
```

## Notes For Reporting

When writing the report, it is best to state:

- the graph was validated using isolated one-minute route tests
- the load was generated with `hey` at about `10 rps`
- metrics were read from ThriveScale’s own `/api/graph`
- client latency from `hey` was used only as an external comparison reference
- the simplified demo namespace was used to finalize the initial functional result because it gives cleaner causal paths than Sock Shop

## Current Status

At the end of this initial FR stage:

- graph correctness is validated on the demo workload
- latency correctness is directionally good on the tested routes
- throughput correctness is acceptable
- the platform-side p90 path is now credible enough to continue to bottleneck-injection testing
