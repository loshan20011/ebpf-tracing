import json
import unittest

try:
    import pandas as pd
    from analyze_eval import compute_system_metrics, service_window_state
except ModuleNotFoundError as exc:  # pragma: no cover - environment guard
    raise unittest.SkipTest(f"optional analysis test dependencies missing: {exc}")


class AnalysisMetricsTest(unittest.TestCase):
    def test_service_window_state_defaults(self):
        state = service_window_state("", "front-end")
        self.assertFalse(state["active"])
        self.assertFalse(state["evaluable"])
        self.assertEqual(state["truth_req_count"], 0)

    def test_compute_system_metrics_skips_inactive_and_unevaluable_services(self):
        rows = [
            {
                "service_p90_json": json.dumps({"front-end": 200.0, "carts": 500.0}),
                "service_state_json": json.dumps(
                    {
                        "front-end": {
                            "active_short": True,
                            "evaluable_for_slo": True,
                            "latency_fresh": True,
                        },
                        "carts": {
                            "active_short": False,
                            "evaluable_for_slo": False,
                            "latency_fresh": False,
                        },
                    }
                ),
                "all_spec_replicas": 4,
                "elapsed_s": 5.0,
            },
            {
                "service_p90_json": json.dumps({"front-end": 120.0, "carts": 50.0}),
                "service_state_json": json.dumps(
                    {
                        "front-end": {
                            "active_short": True,
                            "evaluable_for_slo": True,
                            "latency_fresh": True,
                        },
                        "carts": {
                            "active_short": True,
                            "evaluable_for_slo": False,
                            "latency_fresh": True,
                        },
                    }
                ),
                "all_spec_replicas": 4,
                "elapsed_s": 10.0,
            },
        ]
        df = pd.DataFrame(rows)
        metrics, per_service = compute_system_metrics(
            df,
            {"front-end": 150.0, "carts": 100.0},
        )

        self.assertEqual(per_service["front-end"], 50.0)
        self.assertNotIn("carts", per_service)
        self.assertEqual(metrics["System Active Service Checks"], 2)
        self.assertEqual(metrics["Any Active Service Violation Rate (%)"], 50.0)


if __name__ == "__main__":
    unittest.main()
