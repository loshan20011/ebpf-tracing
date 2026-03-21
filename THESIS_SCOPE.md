# ThriveScale Scope

ThriveScale is scoped as a simple, explainable horizontal autoscaler for stateless microservices.

It does not try to solve every bottleneck class. The thesis scope is:

- QoS-aware autoscaling
- CPU-bottleneck-aware autoscaling
- dependency-aware no-scale decisions
- horizontal scaling only

The controller principle is:

> ThriveScale scales up only when there is service degradation with local CPU pressure, avoids scaling when degradation is dependency-propagated, and scales down when demand is low and service health is stable.

## Controller Outcomes

At each control interval, each service gets only one outcome:

- `scale_up`
- `no_scale`
- `scale_down`

## Decision Classes

The controller only reasons with these classes:

- `local_cpu_pressure`
- `dependency_propagated`
- `low_demand`

## Signals Used

Per service, the controller uses:

- `p90 latency`
- `request rate (RPS)`
- `run queue latency`
- `timeout rate`
- `5xx error rate`
- `downstream dependency latency`
- `current replicas`
- `SLO latency`

Optional supporting signal:

- `CPU utilization`

## Evaluation Scenarios

Use only these three scenarios.

### Scenario 1: Local CPU Pressure

Characteristics:

- high load on a service
- run queue latency rises
- p90 latency rises
- downstream latency is not dominant

Expected controller action:

- `scale_up`

### Scenario 2: Dependency Bottleneck

Characteristics:

- service `A` slows because downstream `B` or `B-db` is slow
- p90 latency rises
- local run queue on `A` stays low
- downstream latency is high

Expected controller action:

- `no_scale`

This is the main differentiator versus HPA.

### Scenario 3: Low Demand

Characteristics:

- traffic drops
- p90 latency is healthy
- run queue latency is low
- extra replicas exist

Expected controller action:

- `scale_down`

## Comparison Against HPA

For each scenario, compare:

- `p90 latency`
- `SLO violation duration`
- `replica count over time`
- `unnecessary scale-ups`
- `pod-minutes` or `replica-seconds` as cost proxy
- `decision correctness`

### Important Interpretation

In Scenario 2, lower SLO violation is not the only success criterion.

If scaling cannot solve the dependency bottleneck, the correct result is:

- ThriveScale avoids unnecessary scaling
- HPA may still scale the wrong service

So the value is better decision correctness and lower wasted cost.

## Explicitly Ignored For This Scope

The thesis controller intentionally does not include:

- panic modes
- multiple controller profiles
- Erlang-C or queueing-model calculations
- complex topology scoring
- blended multi-factor scores
- cluster-wide budget logic
- advanced fallback modes

If a mechanism cannot be explained in one paragraph, it is outside the target controller scope.
