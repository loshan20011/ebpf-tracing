import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RepoLayoutTest(unittest.TestCase):
    def test_canonical_runtime_files_exist(self):
        for rel in [
            "src/agent/agent.py",
            "src/aggregator/aggregator.py",
            "src/aggregator/aggregator_benchmark.py",
            "src/aggregator/aggregator_metrics.py",
            "src/controller/controller.py",
        ]:
            self.assertTrue((ROOT / rel).exists(), rel)

    def test_duplicate_runtime_files_removed(self):
        for rel in [
            "aggregator.py",
            "controller.py",
            "src/aggregator.py",
            "src/controller.py",
            "controller.yaml",
            "set_capacity_profile_env.sh",
        ]:
            self.assertFalse((ROOT / rel).exists(), rel)

    def test_agent_source_contains_expected_parser(self):
        agent_source = (ROOT / "src/agent/agent.py").read_text(encoding="utf-8")
        self.assertIn("def parse_kv_pairs", agent_source)
        self.assertIn("EVENT_RE", agent_source)
        self.assertNotIn("build_legacy_snapshot", agent_source)
        self.assertNotIn('if self.path in ("/", "/metrics")', agent_source)

    def test_default_runtime_is_simple_and_uses_demo_slos(self):
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        controller_source = (ROOT / "src/controller/controller.py").read_text(encoding="utf-8")
        controller_manifest = (ROOT / "deploy/01-system/controller.yaml").read_text(encoding="utf-8")

        self.assertIn("deploy/02-demo-apps/my-slos.yaml", makefile)
        self.assertIn("ACTIVE_RPS_THRESHOLD", controller_manifest)
        self.assertIn("DOWNSCALE_COOLDOWN_S", controller_manifest)
        self.assertNotIn("SCALER_PROFILE", controller_manifest)
        self.assertNotIn("MINIMAL_RUNTIME", controller_manifest)
        self.assertIn('decision = "scale_up"', controller_source)
        self.assertIn('decision = "scale_down"', controller_source)
        self.assertIn('"dependency_propagated"', controller_source)

    def test_system_manifests_target_thrive_scale_namespace(self):
        controller_manifest = (ROOT / "deploy/01-system/controller.yaml").read_text(encoding="utf-8")
        aggregator_manifest = (ROOT / "deploy/01-system/aggregator.yaml").read_text(encoding="utf-8")
        redis_manifest = (ROOT / "deploy/01-system/redis.yaml").read_text(encoding="utf-8")
        frontend_manifest = (ROOT / "deploy/01-system/frontend.yaml").read_text(encoding="utf-8")

        self.assertIn("namespace: thrive-scale", controller_manifest)
        self.assertIn("namespace: thrive-scale", aggregator_manifest)
        self.assertIn("namespace: thrive-scale", redis_manifest)
        self.assertIn("namespace: thrive-scale", frontend_manifest)

    def test_aggregator_defaults_bound_redis_usage(self):
        aggregator_source = (ROOT / "src/aggregator/aggregator.py").read_text(encoding="utf-8")
        aggregator_manifest = (ROOT / "deploy/01-system/aggregator.yaml").read_text(encoding="utf-8")
        redis_manifest = (ROOT / "deploy/01-system/redis.yaml").read_text(encoding="utf-8")

        self.assertIn("BlockingConnectionPool", aggregator_source)
        self.assertIn("REDIS_MAX_CONNECTIONS", aggregator_source)
        self.assertIn("REDIS_STARTUP_RETRIES", aggregator_source)
        self.assertIn('value: "32"', aggregator_manifest)
        self.assertIn('value: "20"', aggregator_manifest)
        self.assertIn('value: "2"', aggregator_manifest)
        self.assertIn('--maxclients", "512"', redis_manifest)

    def test_dashboard_and_support_endpoints_are_present(self):
        aggregator_source = (ROOT / "src/aggregator/aggregator.py").read_text(encoding="utf-8")
        landing_source = (ROOT / "src/frontend/index.html").read_text(encoding="utf-8")
        dashboard_source = (ROOT / "src/frontend/dashboard.html").read_text(encoding="utf-8")
        dockerfile = (ROOT / "src/frontend/Dockerfile").read_text(encoding="utf-8")
        nginx_conf = (ROOT / "src/frontend/nginx.conf").read_text(encoding="utf-8")
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

        self.assertIn('/api/audit', aggregator_source)
        self.assertIn('/api/support/tickets', aggregator_source)
        self.assertIn("detect_benchmark_profile", aggregator_source)
        self.assertIn("trafficBaseUrl", aggregator_source)
        self.assertIn("Research Gaps Addressed", landing_source)
        self.assertIn("Go To Dashboard", landing_source)
        self.assertIn("Contact Us To Set Up ThriveScale", landing_source)
        self.assertIn("Support Desk", dashboard_source)
        self.assertIn("Alerts", dashboard_source)
        self.assertIn("Deployment Context", dashboard_source)
        self.assertIn("Replica Status", dashboard_source)
        self.assertIn('class="hint"', dashboard_source)
        self.assertIn("Service Handling", dashboard_source)
        self.assertIn("Dependency Delay", dashboard_source)
        self.assertIn('location = /dashboard', nginx_conf)
        self.assertIn("COPY dashboard.html", dockerfile)
        self.assertIn("docker build --no-cache -t $(REPO_PREFIX)/frontend:$(TAG) src/frontend", makefile)
        self.assertNotIn('if [ "$$(basename $$f)" = "frontend.yaml" ]', makefile)

    def test_safe_ec2_workflow_is_documented(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        workflow = (ROOT / "EC2_WORKFLOW.md").read_text(encoding="utf-8")
        setup = (ROOT / "SETUP.md").read_text(encoding="utf-8")

        self.assertTrue((ROOT / "EC2_WORKFLOW.md").exists())
        self.assertTrue((ROOT / "SETUP.md").exists())
        self.assertIn("do not use `scp`", readme)
        self.assertIn("See `SETUP.md`", readme)
        self.assertIn("Do not use `scp`.", workflow)
        self.assertIn("Stop remote automation immediately if SSH fails once.", workflow)
        self.assertIn("make deploy-sockshop-demo", setup)
        self.assertIn("make deploy-sockshop-slos", setup)

    def test_demo_comparison_assets_exist(self):
        for rel in [
            "deploy/03-evaluation/hpa-demo.yaml",
            "deploy/03-evaluation/workloads/demo-thrivescale-vs-hpa.yaml",
            "scripts/run_demo_compare.sh",
        ]:
            self.assertTrue((ROOT / rel).exists(), rel)

    def test_worldcup_comparison_assets_exist(self):
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        doc = (ROOT / "WORLDCUP_EVAL.md").read_text(encoding="utf-8")

        for rel in [
            "scripts/worldcup_prepare.py",
            "scripts/run_worldcup_compare.sh",
            "deploy/03-evaluation/workloads/worldcup-sockshop-template.yaml",
            "WORLDCUP_EVAL.md",
        ]:
            self.assertTrue((ROOT / rel).exists(), rel)

        self.assertIn("compare-worldcup", makefile)
        self.assertIn("worldcup_prepare.py", readme)
        self.assertIn("make compare-worldcup", readme)
        self.assertIn("make compare-worldcup", doc)
        self.assertIn("scripts/worldcup_prepare.py", doc)

    def test_sock_shop_route_profiling_assets_exist(self):
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        doc = (ROOT / "SOCKSHOP_ROUTE_PROFILING.md").read_text(encoding="utf-8")

        self.assertTrue((ROOT / "scripts/profile_sockshop_routes.py").exists())
        self.assertTrue((ROOT / "SOCKSHOP_ROUTE_PROFILING.md").exists())
        self.assertIn("profile-sockshop-routes", makefile)
        self.assertIn("profile_sockshop_routes.py", readme)
        self.assertIn("make profile-sockshop-routes", doc)
        self.assertIn("cpu_bound_score", doc)

    def test_thesis_scope_doc_exists(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        doc = (ROOT / "THESIS_SCOPE.md").read_text(encoding="utf-8")

        self.assertTrue((ROOT / "THESIS_SCOPE.md").exists())
        self.assertIn("THESIS_SCOPE.md", readme)
        self.assertIn("Scenario 1: Local CPU Pressure", doc)
        self.assertIn("Scenario 2: Dependency Bottleneck", doc)
        self.assertIn("Scenario 3: Low Demand", doc)
        self.assertIn("local_cpu_pressure", doc)
        self.assertIn("dependency_propagated", doc)
        self.assertIn("low_demand", doc)

    def test_sock_shop_deploy_workflow_is_present(self):
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

        self.assertTrue((ROOT / "scripts/deploy_sock_shop_demo.sh").exists())
        self.assertTrue((ROOT / "scripts/wipe_k3s_demo.sh").exists())
        self.assertIn("deploy-sockshop-demo", makefile)
        self.assertIn("deploy-sockshop-slos", makefile)
        self.assertIn("deploy-sockshop-stack", makefile)
        self.assertIn("wipe-k3s-demo", makefile)


if __name__ == "__main__":
    unittest.main()
