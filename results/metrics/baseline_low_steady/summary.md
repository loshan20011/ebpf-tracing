| Case | Client p90 (ms) | Client RPS | Platform FE p90 (ms) | Platform FE RPS | Metric Accuracy | Expected Path | Observed Path | Dependency Accuracy |
| --- | ---: | ---: | ---: | ---: | --- | --- | --- | --- |
| baseline_low_steady | 19.660 | 0.766 | 5.422 | 1.550 | FAIL | front-end->catalogue, front-end->carts, front-end->user | front-end->carts, front-end->catalogue, front-end->user | PASS |
