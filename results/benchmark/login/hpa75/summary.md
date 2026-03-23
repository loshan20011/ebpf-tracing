# hpa75 Final Benchmark Summary

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

- SLO violation time: 300.0 s
- SLO violation rate: 0.3333
- Time to first action: 50.0
- Recovery time: 250.0
- Error rate: 0.0
- Peak replicas: {'front-end': 10, 'user': 6, 'carts': 9}
- Average replicas: {'front-end': 8.578, 'user': 4.067, 'carts': 5.389}
- Requested CPU core-minutes: {'front-end': 12.8667, 'user': 6.1, 'carts': 24.25}
- Total requested CPU core-minutes: 43.2167
- Replica-seconds: {'front-end': 7720.0, 'user': 3660.0, 'carts': 4850.0}
- Total replica-seconds: 16230.0
- Carts hidden failure indicator: False
- Carts invalid run: False
- Carts restart delta: 0
- Carts log error pattern count: 0
