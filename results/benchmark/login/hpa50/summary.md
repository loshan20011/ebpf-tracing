# hpa50 Final Benchmark Summary

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

- SLO violation time: 180.0 s
- SLO violation rate: 0.2
- Time to first action: 40.0
- Recovery time: 70.0
- Error rate: 0.0
- Peak replicas: {'front-end': 10, 'user': 7, 'carts': 13}
- Average replicas: {'front-end': 9.033, 'user': 5.278, 'carts': 8.378}
- Requested CPU core-minutes: {'front-end': 13.55, 'user': 7.9167, 'carts': 37.7}
- Total requested CPU core-minutes: 59.1667
- Replica-seconds: {'front-end': 8130.0, 'user': 4750.0, 'carts': 7540.0}
- Total replica-seconds: 20420.0
- Carts hidden failure indicator: False
- Carts invalid run: False
- Carts restart delta: 0
- Carts log error pattern count: 0
