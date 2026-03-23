#!/usr/bin/env python3
import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import urllib.request
from pathlib import Path

from patch_demo_gateway_slo import prepare_sock_shop_slos, prepare_thrive_demo_slos


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def fetch_json(url: str) -> dict | list | None:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return json.load(response)
    except urllib.error.URLError:
        return None
    except Exception:
        return None


def append_ndjson(path: Path, payload: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, separators=(",", ":")) + "\n")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def request_json(url: str, method: str = "GET", payload: dict | None = None, headers: dict | None = None) -> dict | list | None:
    data = None
    request_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url=url, data=data, method=method.upper(), headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.URLError:
        return None
    except Exception:
        return None


def kubectl_cmd(args: list[str]) -> list[str] | None:
    kubectl = shutil.which("kubectl")
    if kubectl:
        return [kubectl, *args]
    k3s = shutil.which("k3s")
    if k3s:
        return [k3s, "kubectl", *args]
    return None


def kubectl_json(args: list[str]) -> dict | list | None:
    cmd = kubectl_cmd(args)
    if not cmd:
        return None
    try:
        output = subprocess.check_output(cmd, text=True)
        return json.loads(output)
    except Exception:
        return None


def kubectl_ok(args: list[str]) -> bool:
    cmd = kubectl_cmd(args)
    if not cmd:
        return False
    try:
        subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def kubectl_output(args: list[str]) -> str | None:
    cmd = kubectl_cmd(args)
    if not cmd:
        return None
    try:
        return subprocess.check_output(cmd, text=True)
    except Exception:
        return None


def scale_deployment(namespace: str, deployment: str, replicas: int) -> bool:
    return kubectl_ok(["scale", "deployment", deployment, "-n", namespace, f"--replicas={int(replicas)}"])


def restart_deployment(namespace: str, deployment: str) -> bool:
    return kubectl_ok(["rollout", "restart", "deployment", deployment, "-n", namespace])


def set_workload_env(kind: str, namespace: str, name: str, env_map: dict[str, str]) -> bool:
    if not env_map:
        return True
    args = ["set", "env", f"{kind}/{name}", "-n", namespace]
    args.extend([f"{key}={value}" for key, value in env_map.items()])
    return kubectl_ok(args)


def desired_ready_map(deployments_payload: dict | list | None) -> dict[str, tuple[int, int]]:
    items = deployments_payload.get("items", []) if isinstance(deployments_payload, dict) else []
    out: dict[str, tuple[int, int]] = {}
    for item in items:
        name = item.get("metadata", {}).get("name")
        if not name:
            continue
        spec = int(item.get("spec", {}).get("replicas", 0) or 0)
        ready = int(item.get("status", {}).get("readyReplicas", 0) or 0)
        out[str(name)] = (spec, ready)
    return out


def wait_for_deployments(namespace: str, expected: dict[str, int], timeout_seconds: int) -> bool:
    deadline = time.time() + max(5, timeout_seconds)
    while time.time() < deadline:
        payload = kubectl_json(["get", "deploy", "-n", namespace, "-o", "json"])
        status = desired_ready_map(payload)
        if all(status.get(name) == (replicas, replicas) for name, replicas in expected.items()):
            return True
        time.sleep(2)
    return False


def set_controller_mode(system_namespace: str, controller_deployment: str, mode: str) -> int:
    replicas = 0 if mode == "observation" else 1
    scale_deployment(system_namespace, controller_deployment, replicas)
    return replicas


def infer_root_service(workload_namespace: str, case_cfg: dict) -> str:
    analysis = case_cfg.get("analysis") if isinstance(case_cfg, dict) else {}
    if isinstance(analysis, dict):
        root_service = str(analysis.get("root_service", "") or "").strip()
        if root_service:
            return root_service
    return "gateway" if workload_namespace == "thrive-demo" else "front-end"


def set_system_target(
    system_namespace: str,
    workload_namespace: str,
    root_service: str,
    controller_deployment: str,
) -> dict[str, dict[str, str]]:
    aggregator_env = {
        "TARGET_NAMESPACE": workload_namespace,
    }
    agent_env = {
        "TARGET_NAMESPACE": workload_namespace,
    }
    controller_env = {
        "TARGET_NAMESPACE": workload_namespace,
        "ROOT_SERVICE": root_service,
    }
    set_workload_env("deployment", system_namespace, "aggregator", aggregator_env)
    set_workload_env("daemonset", system_namespace, "bpf-agent", agent_env)
    set_workload_env("deployment", system_namespace, controller_deployment, controller_env)
    return {
        "aggregator": aggregator_env,
        "bpf_agent": agent_env,
        "controller": controller_env,
    }


def set_observation_thresholds(system_namespace: str, mode: str, controller_deployment: str) -> dict[str, dict[str, str]]:
    if mode == "observation":
        aggregator_env = {
            "FUNCTIONAL_TEST_MODE": "true",
            "EBPF_REQ_MIN_COUNT": "5",
            "EBPF_REQ_MIN_RPS": "0.25",
            "EBPF_REQ_MIN_NET_SAMPLES": "5",
            "EBPF_REQ_MIN_RUNQ_SAMPLES": "5",
        }
        agent_env = {
            "FUNCTIONAL_TEST_MODE": "true",
            "CMDLINE_SERVICE_FALLBACK_ENABLED": "false",
            "RUNQ_MIN_US": "2000",
        }
        controller_env = {}
    else:
        aggregator_env = {
            "FUNCTIONAL_TEST_MODE": "false",
            "EBPF_REQ_MIN_COUNT": "3",
            "EBPF_REQ_MIN_RPS": "1.0",
            "EBPF_REQ_MIN_NET_SAMPLES": "3",
            "EBPF_REQ_MIN_RUNQ_SAMPLES": "3",
        }
        agent_env = {
            "FUNCTIONAL_TEST_MODE": "false",
            "CMDLINE_SERVICE_FALLBACK_ENABLED": "true",
            "RUNQ_MIN_US": "250",
        }
        controller_env = {
            "PRIMARY_BREACH_STREAK_REQUIRED": "1",
            "PRIMARY_CONFIDENT_FIRST_UPSCALE_STEP": "3",
            "PRIMARY_CONFIDENT_UPSCALE_RATIO": "1.05",
            "PRIMARY_REACTIVE_MIN_UPSCALE_STEP": "2",
            "PRIMARY_REACTIVE_SEVERE_UPSCALE_STEP": "3",
            "PRIMARY_SEVERE_BREACH_RATIO": "1.35",
            "PRIMARY_UPSCALE_COOLDOWN_S": "0",
            "UPSCALE_COOLDOWN_S": "0",
            "PRIMARY_TARGET_STICKY_SECONDS": "20",
        }
    set_workload_env("deployment", system_namespace, "aggregator", aggregator_env)
    set_workload_env("daemonset", system_namespace, "bpf-agent", agent_env)
    set_workload_env("deployment", system_namespace, controller_deployment, controller_env)
    return {
        "aggregator": aggregator_env,
        "bpf_agent": agent_env,
        "controller": controller_env,
    }


def wait_for_rollout(kind: str, namespace: str, name: str, timeout_seconds: int) -> bool:
    timeout_value = max(10, int(timeout_seconds))
    return kubectl_ok(["rollout", "status", f"{kind}/{name}", "-n", namespace, f"--timeout={timeout_value}s"])


def prepare_case_environment(
    args,
    session_meta: dict,
) -> None:
    case_cfg = load_json(args.case_config) if args.case_config else {}
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
    restart_after_reset = bool(case_cfg.get("restart_after_reset", args.namespace == "thrive-demo"))

    session_meta["mode"] = args.mode
    session_meta["case_config"] = str(args.case_config) if args.case_config else ""
    session_meta["requested_initial_replicas"] = initial_replicas
    session_meta["restart_after_reset"] = restart_after_reset
    root_service = infer_root_service(args.namespace, case_cfg)
    session_meta["system_target"] = set_system_target(
        args.system_namespace,
        args.namespace,
        root_service,
        args.controller_deployment,
    )
    session_meta["aggregator_observation_thresholds"] = set_observation_thresholds(
        args.system_namespace, args.mode, args.controller_deployment
    )
    wait_for_rollout("deployment", args.system_namespace, "aggregator", args.prepare_timeout_seconds)
    wait_for_rollout("daemonset", args.system_namespace, "bpf-agent", args.prepare_timeout_seconds)
    session_meta["controller_target_replicas"] = set_controller_mode(
        args.system_namespace, args.controller_deployment, args.mode
    )
    if session_meta["controller_target_replicas"] > 0:
        wait_for_rollout("deployment", args.system_namespace, args.controller_deployment, args.prepare_timeout_seconds)

    wait_for_deployments(args.system_namespace, {"aggregator": 1}, args.prepare_timeout_seconds)

    reset_response = request_json(f"{args.aggregator_base_url.rstrip('/')}/api/reset")
    session_meta["reset_response"] = reset_response or {"ok": False}
    if args.namespace == "thrive-demo":
        slo_setup = prepare_thrive_demo_slos(case_cfg, namespace=args.namespace)
        session_meta["slo_setup"] = slo_setup
    elif args.namespace == "sock-shop":
        slo_setup = prepare_sock_shop_slos(case_cfg, namespace=args.namespace)
        session_meta["slo_setup"] = slo_setup

    for deployment, replicas in initial_replicas.items():
        scale_deployment(args.namespace, deployment, replicas)

    # Force fresh outbound connections after each aggregator reset so topology
    # edges can be rediscovered even when workloads normally reuse keep-alive
    # sockets across requests.
    restarted = []
    if restart_after_reset:
        for deployment in initial_replicas:
            if restart_deployment(args.namespace, deployment):
                restarted.append(deployment)
        for deployment in restarted:
            wait_for_rollout("deployment", args.namespace, deployment, args.prepare_timeout_seconds)

    wait_targets = dict(initial_replicas)
    wait_targets[args.controller_deployment] = session_meta["controller_target_replicas"]
    stabilized = wait_for_deployments(args.namespace, initial_replicas, args.prepare_timeout_seconds)
    controller_ready = wait_for_deployments(
        args.system_namespace,
        {args.controller_deployment: session_meta["controller_target_replicas"]},
        args.prepare_timeout_seconds,
    )
    session_meta["replicas_restored"] = bool(stabilized)
    session_meta["controller_mode_applied"] = bool(controller_ready)
    session_meta["restarted_deployments"] = restarted
    session_meta["prepared_at_unix_ms"] = int(time.time() * 1000)
    time.sleep(max(0, args.stabilization_seconds))
    session_meta["collection_started_at_unix_ms"] = int(time.time() * 1000)


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect functional evaluation snapshots.")
    parser.add_argument("--case-name", required=True, help="Logical case name")
    parser.add_argument(
        "--output-root",
        default=Path("results/metrics"),
        type=Path,
        help="Root output directory",
    )
    parser.add_argument(
        "--aggregator-base-url",
        default="http://127.0.0.1:30938",
        help="Base URL for the ThriveScale aggregator",
    )
    parser.add_argument(
        "--namespace",
        default="sock-shop",
        help="Kubernetes namespace for deployment replica collection",
    )
    parser.add_argument(
        "--duration-seconds",
        type=int,
        default=540,
        help="How long to collect data",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=5,
        help="Polling interval in seconds",
    )
    parser.add_argument(
        "--case-config",
        type=Path,
        help="Optional case config JSON used for replica reset metadata",
    )
    parser.add_argument(
        "--mode",
        choices=["observation", "control"],
        default="observation",
        help="Observation disables autoscaling by scaling the controller to zero replicas",
    )
    parser.add_argument(
        "--system-namespace",
        default="thrive-scale",
        help="Namespace where ThriveScale control-plane deployments run",
    )
    parser.add_argument(
        "--controller-deployment",
        default="custom-autoscaler",
        help="Controller deployment name to enable/disable for observation or control mode",
    )
    parser.add_argument(
        "--stabilization-seconds",
        type=int,
        default=20,
        help="Extra settle time after reset and replica restore before collection begins",
    )
    parser.add_argument(
        "--prepare-timeout-seconds",
        type=int,
        default=120,
        help="Maximum time to wait for restored replicas and controller mode to become ready",
    )
    args = parser.parse_args()

    case_dir = args.output_root / args.case_name
    ensure_dir(case_dir)

    graph_path = case_dir / "aggregator_graph.ndjson"
    traces_path = case_dir / "controller_traces.ndjson"
    audit_path = case_dir / "controller_audit.ndjson"
    control_state_path = case_dir / "control_state.ndjson"
    replica_path = case_dir / "replica_counts.ndjson"
    session_path = case_dir / "collector_session.json"

    session_meta = {
        "case_name": args.case_name,
        "aggregator_base_url": args.aggregator_base_url,
        "namespace": args.namespace,
        "duration_seconds": args.duration_seconds,
        "interval_seconds": args.interval_seconds,
        "started_at_unix_ms": int(time.time() * 1000),
    }
    prepare_case_environment(args, session_meta)
    session_path.write_text(json.dumps(session_meta, indent=2, sort_keys=True), encoding="utf-8")

    end_time = time.time() + args.duration_seconds
    while time.time() < end_time:
        ts_ms = int(time.time() * 1000)
        graph = fetch_json(f"{args.aggregator_base_url.rstrip('/')}/api/graph")
        traces = fetch_json(f"{args.aggregator_base_url.rstrip('/')}/api/traces?limit=100")
        audit = fetch_json(f"{args.aggregator_base_url.rstrip('/')}/api/audit")
        control_state = fetch_json(f"{args.aggregator_base_url.rstrip('/')}/api/control/state")
        deployments = kubectl_json(["get", "deploy", "-n", args.namespace, "-o", "json"])

        append_ndjson(graph_path, {"ts_unix_ms": ts_ms, "payload": graph})
        append_ndjson(traces_path, {"ts_unix_ms": ts_ms, "payload": traces})
        append_ndjson(audit_path, {"ts_unix_ms": ts_ms, "payload": audit})
        append_ndjson(control_state_path, {"ts_unix_ms": ts_ms, "payload": control_state})
        append_ndjson(replica_path, {"ts_unix_ms": ts_ms, "payload": deployments})

        time.sleep(max(1, args.interval_seconds))

    session_meta["collection_ended_at_unix_ms"] = int(time.time() * 1000)
    session_path.write_text(json.dumps(session_meta, indent=2, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
