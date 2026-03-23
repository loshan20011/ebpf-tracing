# thrivescale Final Benchmark Summary

## Workload

```json
[
  {
    "duration_seconds": 120,
    "name": "phase_a",
    "rps": 100
  },
  {
    "duration_seconds": 180,
    "name": "phase_b",
    "rps": 150
  },
  {
    "duration_seconds": 240,
    "name": "phase_c",
    "rps": 250
  },
  {
    "duration_seconds": 180,
    "name": "phase_d",
    "rps": 175
  },
  {
    "duration_seconds": 180,
    "name": "phase_e",
    "rps": 325
  }
]
```

## Metrics

- SLO violation time: 220.0 s
- SLO violation rate: 0.2444
- Time to first action: 30.0
- Recovery time: 220.0
- Error rate: 0.0
- Peak replicas: {'front-end': 10, 'user': 13, 'carts': 13}
- Average replicas: {'front-end': 6.7, 'user': 8.467, 'carts': 8.333}
- Requested CPU core-minutes: {'front-end': 10.05, 'user': 12.7, 'carts': 37.5}
- Total requested CPU core-minutes: 60.25
- Replica-seconds: {'front-end': 6030.0, 'user': 7620.0, 'carts': 7500.0}
- Total replica-seconds: 21150.0
- Carts hidden failure indicator: False
- Carts invalid run: False
- Carts restart delta: 0
- Carts log error pattern count: 0
