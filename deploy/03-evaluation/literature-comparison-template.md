# Literature Comparison Template (Not Direct Reproduction)

Use this table to contextualize ThriveScale Sock Shop results against published PBScaler/MicroScaler/SHOWAR numbers.

Important note: values from prior papers are **not reproduced in this cluster/testbed** unless explicitly marked.

| Method | Benchmark | Workload | SLO Definition | Violation Definition | Cost Unit | Reported Violation (%) | Reported Cost |
|---|---|---|---|---|---|---:|---:|
| ThriveScale | Sock Shop | Burst (this thesis) | p90 <= 150ms | p90 sample > SLO | Replica-seconds | TBD | TBD |
| HPA | Sock Shop | Burst (this thesis) | p90 <= 150ms | p90 sample > SLO | Replica-seconds | TBD | TBD |
| ThriveScale | Sock Shop | World Cup-style replay (this thesis) | per ServiceSLO | active sample p90 > SLO | Replica-seconds | TBD | TBD |
| HPA | Sock Shop | World Cup-style replay (this thesis) | per ServiceSLO | active sample p90 > SLO | Replica-seconds | TBD | TBD |
| PBScaler | (paper) | EW1..EW5 | per paper | per paper | per paper | from paper | from paper |
| MicroScaler | (paper) | EW1..EW5 | per paper | per paper | per paper | from paper | from paper |
| SHOWAR | (paper) | EW1..EW5 | per paper | per paper | per paper | from paper | from paper |

Before claiming superiority against external methods, normalize or clearly disclaim differences in testbed, trace set, and cost units.
