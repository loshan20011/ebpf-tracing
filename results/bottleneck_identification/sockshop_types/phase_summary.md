| Case | Expected Service | Actual Service | Service Pass | Service Stable | Expected Reason | Evaluated Reason | Reason Pass | Reason Stable | Expected Path | Path Reason | Path Pass | Expected Leaf | Leaf Reason | Leaf Pass | Overall |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| SSBR1_local_bottleneck_catalogue | catalogue | catalogue | PASS | FAIL | local_bottleneck | local_bottleneck | PASS | FAIL |  | None |  | local_unclear_or_non_cpu | local_unclear_or_non_cpu | PASS | FAIL |
| SSBR2_downstream_delay_customers | user | user | PASS | PASS | downstream_delay | downstream_delay | PASS | PASS | downstream_delay | downstream_delay | PASS |  | local_cpu_pressure |  | PASS |
| SSBR3_external_or_unmonitored_customers | front-end | front-end | PASS | FAIL | external_or_unmonitored_delay | local_bottleneck | FAIL | FAIL |  | None |  | external_or_unmonitored_delay | local_unclear_or_non_cpu | FAIL | FAIL |
