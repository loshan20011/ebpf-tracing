# Functional Metric Tests

This matrix is for functional validation only.

Scope:
- metric correctness
- graph correctness
- bottleneck correctness
- controller correctness

Rules:
- keep tests route-isolated and low traffic first
- use frontend latency as the main SLO trigger
- use runq only as local-pressure support
- do not use large real trace replay yet
- do not compare against HPA yet
- run metric and graph validation in `observation` mode first
- reset aggregator state and restore known replicas before every case

## Baseline Profile

Baseline case:
- config: `src/scripts/metrics/cases/baseline_low_steady.json`
- duration: 9 minutes
- traffic: low steady mixed Sock Shop traffic
- goal: capture healthy frontend p90, service p90s, RPS ranges, runq range, and stable graph edges

Expected baseline outputs:
- healthy frontend p90
- healthy p90 per monitored service
- healthy RPS values
- normal runq range
- stable graph edges per route
- raw request logs
- raw graph snapshots
- raw traces and replica snapshots

Execution note:
- use `observation` mode for baseline and F1/F3/F4
- only rerun `F2_graph_login` after the login route is fixed to return successful responses in the chosen test flow

## Test Cases

### F1_graph_catalogue

Purpose:
- verify the platform observes the catalogue path correctly

Route:
- `GET /catalogue`

Injected condition:
- none

Expected graph:
- `front-end -> catalogue`

Expected bottleneck:
- none under healthy low load

Expected controller action:
- hold

Pass/fail evidence:
- collector graph snapshots show `front-end -> catalogue`
- client p90 and platform p90 are directionally close for the same window
- no incorrect downstream edges dominate the route

### F2_graph_login

Purpose:
- verify the platform observes the login path correctly

Route:
- `GET /login`

Injected condition:
- none

Expected graph:
- `front-end -> user`

Expected bottleneck:
- none under healthy low load

Expected controller action:
- hold

Pass/fail evidence:
- graph shows `front-end -> user`
- client and platform latency remain directionally aligned
- controller traces do not target unrelated services

### F3_graph_cart

Purpose:
- verify the platform observes the cart read path correctly

Route:
- `GET /cart`

Injected condition:
- none

Expected graph:
- `front-end -> carts`

Expected bottleneck:
- none under healthy low load

Expected controller action:
- hold

Pass/fail evidence:
- graph shows `front-end -> carts`
- carts becomes active for the route window
- no dependency misclassification is introduced

### F4_graph_post_cart

Purpose:
- verify the platform observes the dependent cart write path correctly

Route:
- `POST /cart`

Injected condition:
- none

Expected graph:
- `front-end -> catalogue -> carts`

Expected bottleneck:
- none under healthy low load

Expected controller action:
- hold

Pass/fail evidence:
- graph shows both `front-end -> catalogue` and `front-end -> carts` or a route-consistent dependency chain
- catalogue and carts both become active during the isolated route window
- dependency attribution is directionally correct for the write path

### F5_local_bottleneck

Purpose:
- verify the system identifies a real local bottleneck on a monitored service

Route:
- route should exercise the selected monitored service directly

Injected condition:
- apply local CPU pressure to one monitored stateless service

Expected graph:
- route path remains correct

Expected bottleneck:
- the stressed monitored service

Expected controller action:
- targeted bounded scale-up of that monitored service

Pass/fail evidence:
- runq rises mainly on the stressed service
- service_handling_latency rises on the stressed service
- dependency_attributed_latency and external_wait_latency stay low
- controller trace targets that service, not an unrelated parent or sibling

### F6_external_hold

Purpose:
- verify the system avoids useless scaling when delay comes from an unmonitored dependency

Route:
- route should traverse a monitored service that depends on an unmonitored downstream system

Injected condition:
- add delay to the unmonitored dependency

Expected graph:
- upstream path remains visible

Expected bottleneck:
- external or unmonitored downstream hold case

Expected controller action:
- hold

Pass/fail evidence:
- upstream external wait rises
- upstream dependency-attributed latency rises where appropriate
- no monitored service is scaled for the external delay alone

## Metric Validation Checks

### Latency Correctness

Method:
- compute client-side p90 from `request_log.ndjson`
- compare against platform p90 for the same route window

Expected:
- values do not need to match exactly, but should be directionally reasonable

### Throughput Correctness

Method:
- compute RPS from successful requests and phase duration
- compare against platform-reported RPS

Expected:
- same order of magnitude under isolated route tests

### RunQ Correctness

Method:
- create one CPU-heavy local bottleneck case

Expected:
- runq rises mainly on the stressed service
- unrelated services should not show the same pattern

### Service Handling Correctness

Method:
- create one local-only bottleneck case

Expected:
- `service_handling_latency` rises
- `dependency_attributed_latency` stays low
- `external_wait_latency` stays low

### Dependency-Attributed Latency Correctness

Method:
- create one downstream bottleneck case on a monitored child

Expected:
- upstream `dependency_attributed_latency` rises
- controller traversal targets the child service

### External Wait Correctness

Method:
- create one delayed unmonitored dependency case

Expected:
- `external_wait_latency` rises
- controller holds instead of useless scale-up

### Graph Correctness

Required route-path checks:
- `GET /catalogue -> front-end -> catalogue`
- `GET /login -> front-end -> user`
- `GET /cart -> front-end -> carts`
- `POST /cart -> front-end -> catalogue -> carts`
