#!/usr/bin/env python3
import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent
COMMON_DIR = REPO_ROOT / "src" / "scripts" / "common"
sys.path.insert(0, str(COMMON_DIR))

from collect_metrics import request_json  # type: ignore
from patch_demo_gateway_slo import prepare_sock_shop_slos  # type: ignore

sys.path.insert(0, str(COMMON_DIR))
from run_bottleneck_case import ensure_dir, load_json, run_traffic, wait_for_collection_start  # type: ignore


DEFAULT_NAMESPACE = "sock-shop"
DEFAULT_SYSTEM_NAMESPACE = "thrive-scale"
DEFAULT_CONTROLLER_DEPLOYMENT = "custom-autoscaler"
DEFAULT_AGG_URL = "http://127.0.0.1:30938"


def kubectl_cmd(args: list[str]) -> list[str]:
    kubectl = shutil.which("kubectl")
    if kubectl:
        return [kubectl, *args]
    k3s = shutil.which("k3s")
    if k3s:
        return [k3s, "kubectl", *args]
    raise RuntimeError("kubectl or k3s not found")


def kubectl_ok(args: list[str]) -> bool:
    try:
        subprocess.check_call(kubectl_cmd(args), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def kubectl_json(args: list[str]) -> dict:
    output = subprocess.check_output(kubectl_cmd(args), text=True)
    return json.loads(output)


def wait_for_rollout(kind: str, namespace: str, name: str, timeout_seconds: int) -> None:
    subprocess.check_call(
        kubectl_cmd(["rollout", "status", f"{kind}/{name}", "-n", namespace, f"--timeout={max(10, timeout_seconds)}s"])
    )


def scale_deployment(namespace: str, deployment: str, replicas: int) -> None:
    subprocess.check_call(kubectl_cmd(["scale", "deployment", deployment, "-n", namespace, f"--replicas={int(replicas)}"]))


def restart_deployment(namespace: str, deployment: str) -> None:
    subprocess.check_call(kubectl_cmd(["rollout", "restart", f"deployment/{deployment}", "-n", namespace]))


def set_env(kind: str, namespace: str, name: str, env_map: dict[str, str]) -> None:
    if not env_map:
        return
    args = ["set", "env", f"{kind}/{name}", "-n", namespace]
    args.extend([f"{key}={value}" for key, value in env_map.items()])
    subprocess.check_call(kubectl_cmd(args))


def wait_for_deployments(namespace: str, expected: dict[str, int], timeout_seconds: int) -> None:
    deadline = time.time() + max(10, timeout_seconds)
    while time.time() < deadline:
        payload = kubectl_json(["get", "deploy", "-n", namespace, "-o", "json"])
        items = payload.get("items", []) if isinstance(payload, dict) else []
        status = {}
        for item in items:
            name = str(item.get("metadata", {}).get("name", ""))
            status[name] = (
                int(item.get("spec", {}).get("replicas", 0) or 0),
                int(item.get("status", {}).get("readyReplicas", 0) or 0),
            )
        if all(status.get(name) == (replicas, replicas) for name, replicas in expected.items()):
            return
        time.sleep(2)
    raise TimeoutError(f"deployments not ready in namespace {namespace}: {expected}")


def apply_fixed_resources(namespace: str) -> None:
    subprocess.check_call(
        kubectl_cmd(
            [
                "-n",
                namespace,
                "set",
                "resources",
                "deployment/front-end",
                "--requests=cpu=100m,memory=128Mi",
                "--limits=cpu=500m,memory=256Mi",
            ]
        )
    )
    subprocess.check_call(
        kubectl_cmd(
            [
                "-n",
                namespace,
                "set",
                "resources",
                "deployment/user",
                "--requests=cpu=100m,memory=128Mi",
                "--limits=cpu=300m,memory=256Mi",
            ]
        )
    )
    subprocess.check_call(
        kubectl_cmd(
            [
                "-n",
                namespace,
                "set",
                "resources",
                "deployment/carts",
                "--requests=cpu=300m,memory=512Mi",
                "--limits=cpu=600m,memory=1Gi",
            ]
        )
    )
    subprocess.check_call(
        kubectl_cmd(
            [
                "-n",
                namespace,
                "set",
                "resources",
                "deployment/catalogue",
                "--requests=cpu=100m,memory=128Mi",
                "--limits=cpu=300m,memory=256Mi",
            ]
        )
    )


def delete_hpas(namespace: str) -> None:
    kubectl_ok(["delete", "hpa", "front-end", "user", "carts", "catalogue", "-n", namespace, "--ignore-not-found"])


def apply_hpa_profile(namespace: str, cpu_target: int) -> None:
    delete_hpas(namespace)
    subprocess.check_call(kubectl_cmd(["-n", namespace, "autoscale", "deployment", "front-end", f"--cpu-percent={cpu_target}", "--min=1", "--max=10"]))
    subprocess.check_call(kubectl_cmd(["-n", namespace, "autoscale", "deployment", "user", f"--cpu-percent={cpu_target}", "--min=1", "--max=13"]))
    subprocess.check_call(kubectl_cmd(["-n", namespace, "autoscale", "deployment", "carts", f"--cpu-percent={cpu_target}", "--min=1", "--max=13"]))
    subprocess.check_call(kubectl_cmd(["-n", namespace, "autoscale", "deployment", "catalogue", f"--cpu-percent={cpu_target}", "--min=1", "--max=8"]))


def set_controller_replicas(system_namespace: str, deployment: str, replicas: int) -> None:
    scale_deployment(system_namespace, deployment, replicas)
    if replicas > 0:
        wait_for_rollout("deployment", system_namespace, deployment, 300)


def clear_thrive_state(system_namespace: str) -> None:
    subprocess.check_call(kubectl_cmd(["-n", system_namespace, "exec", "deploy/redis", "--", "redis-cli", "FLUSHDB"]), stdout=subprocess.DEVNULL)


def configure_system_targets(system_namespace: str, namespace: str, root_service: str, controller_deployment: str) -> None:
    set_env("deployment", system_namespace, "aggregator", {"TARGET_NAMESPACE": namespace})
    set_env("daemonset", system_namespace, "bpf-agent", {"TARGET_NAMESPACE": namespace})
    set_env("deployment", system_namespace, controller_deployment, {"TARGET_NAMESPACE": namespace, "ROOT_SERVICE": root_service})


def prepare_arm(case_cfg: dict, arm: str, namespace: str, system_namespace: str, controller_deployment: str, aggregator_base_url: str) -> None:
    analysis = case_cfg.get("analysis") if isinstance(case_cfg, dict) else {}
    root_service = str((analysis or {}).get("root_service", "front-end")).strip() or "front-end"
    initial_replicas = {
        str(name): int(replicas)
        for name, replicas in (case_cfg.get("initial_replicas") or {}).items()
    }
    if not initial_replicas:
        initial_replicas = {
            "front-end": 1,
            "catalogue": 1,
            "carts": 1,
            "orders": 1,
            "user": 1,
            "payment": 1,
            "shipping": 1,
        }

    configure_system_targets(system_namespace, namespace, root_service, controller_deployment)
    wait_for_rollout("deployment", system_namespace, "aggregator", 300)
    wait_for_rollout("daemonset", system_namespace, "bpf-agent", 300)
    apply_fixed_resources(namespace)
    delete_hpas(namespace)
    clear_thrive_state(system_namespace)
    request_json(f"{aggregator_base_url.rstrip('/')}/api/reset", method="POST")

    if namespace == DEFAULT_NAMESPACE:
        slo_setup = prepare_sock_shop_slos(case_cfg, namespace=namespace)
        if not slo_setup.get("ok"):
            raise RuntimeError(f"failed to prepare sock-shop ServiceSLOs: {slo_setup}")

    if arm == "noautoscale":
        set_controller_replicas(system_namespace, controller_deployment, 0)
    elif arm == "hpa50":
        set_controller_replicas(system_namespace, controller_deployment, 0)
        apply_hpa_profile(namespace, 50)
    elif arm == "hpa70":
        set_controller_replicas(system_namespace, controller_deployment, 0)
        apply_hpa_profile(namespace, 70)
    elif arm == "thrivescale":
        delete_hpas(namespace)
        set_controller_replicas(system_namespace, controller_deployment, 1)
        subprocess.check_call(kubectl_cmd(["rollout", "restart", f"deployment/{controller_deployment}", "-n", system_namespace]))
        wait_for_rollout("deployment", system_namespace, controller_deployment, 300)
    else:
        raise ValueError(f"unknown arm: {arm}")

    for deployment, replicas in initial_replicas.items():
        scale_deployment(namespace, deployment, replicas)

    if bool(case_cfg.get("restart_after_reset", True)):
        for deployment in initial_replicas:
            restart_deployment(namespace, deployment)
        for deployment in initial_replicas:
            wait_for_rollout("deployment", namespace, deployment, 300)

    wait_for_deployments(namespace, initial_replicas, 300)
    time.sleep(max(0, int(case_cfg.get("stabilization_seconds", 20))))


def run_case(case_config: Path, output_root: Path, arm: str, aggregator_base_url: str) -> int:
    case_cfg = load_json(case_config)
    case_name = str(case_cfg.get("case_name") or case_config.stem)
    namespace = str(case_cfg.get("namespace", DEFAULT_NAMESPACE))
    system_namespace = str(case_cfg.get("system_namespace", DEFAULT_SYSTEM_NAMESPACE))
    controller_deployment = str(case_cfg.get("controller_deployment", DEFAULT_CONTROLLER_DEPLOYMENT))
    case_dir = output_root / arm / case_name
    ensure_dir(case_dir)
    (case_dir / "case_config.json").write_text(json.dumps(case_cfg, indent=2, sort_keys=True), encoding="utf-8")

    prepare_arm(case_cfg, arm, namespace, system_namespace, controller_deployment, aggregator_base_url)

    collector_cmd = [
        sys.executable,
        str(COMMON_DIR / "collect_metrics.py"),
        "--case-name",
        case_name,
        "--output-root",
        str(output_root / arm),
        "--aggregator-base-url",
        aggregator_base_url,
        "--namespace",
        namespace,
        "--duration-seconds",
        str(int(case_cfg.get("collector_duration_seconds", case_cfg.get("duration_seconds", 90)))),
        "--interval-seconds",
        str(int(case_cfg.get("interval_seconds", 5))),
        "--case-config",
        str(case_config),
        "--mode",
        "control" if arm == "thrivescale" else "observation",
        "--system-namespace",
        system_namespace,
        "--controller-deployment",
        controller_deployment,
        "--stabilization-seconds",
        "0",
        "--skip-prepare",
    ]

    collector_proc = subprocess.Popen(collector_cmd)
    try:
        wait_for_collection_start(case_dir, timeout_seconds=60)
        run_traffic(case_cfg, case_dir)
    finally:
        collector_rc = collector_proc.wait()
        if collector_rc != 0:
            raise RuntimeError(f"collector failed with exit code {collector_rc}")

    subprocess.check_call(
        [
            sys.executable,
            str(SCRIPT_DIR / "summarize_autoscaler_scenario_case.py"),
            "--case-dir",
            str(case_dir),
            "--arm",
            arm,
        ]
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one Sock Shop autoscaler scenario case for one autoscaler arm.")
    parser.add_argument("--case-config", required=True, type=Path, help="Case config JSON")
    parser.add_argument("--output-root", required=True, type=Path, help="Scenario results root")
    parser.add_argument("--arm", required=True, choices=["noautoscale", "hpa50", "hpa70", "thrivescale"])
    parser.add_argument("--aggregator-base-url", default=DEFAULT_AGG_URL, help="Aggregator base URL")
    args = parser.parse_args()
    return run_case(args.case_config, args.output_root, args.arm, args.aggregator_base_url)


if __name__ == "__main__":
    sys.exit(main())
