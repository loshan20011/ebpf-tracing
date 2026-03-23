#!/usr/bin/env python3
import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path


DEFAULT_DOWNSTREAM_SLOS = {
    "cpu-slo": {"targetDeployment": "svc-cpu", "sloLatency": 10.0, "minReplicas": 1, "maxReplicas": 5, "priority": "secondary"},
    "chain-slo": {"targetDeployment": "svc-chain", "sloLatency": 12.0, "minReplicas": 1, "maxReplicas": 5, "priority": "secondary"},
    "io-slo": {"targetDeployment": "svc-io", "sloLatency": 100.0, "minReplicas": 1, "maxReplicas": 5, "priority": "secondary"},
    "fanout-slo": {"targetDeployment": "svc-fanout", "sloLatency": 100.0, "minReplicas": 1, "maxReplicas": 5, "priority": "secondary"},
}

DEFAULT_SOCKSHOP_SLOS = {
    "sockshop-front-end-slo": {"targetDeployment": "front-end", "sloLatency": 3.0, "minReplicas": 1, "maxReplicas": 8, "priority": "primary"},
    "sockshop-catalogue-slo": {"targetDeployment": "catalogue", "sloLatency": 3.0, "minReplicas": 1, "maxReplicas": 8, "priority": "secondary"},
    "sockshop-carts-slo": {"targetDeployment": "carts", "sloLatency": 150.0, "minReplicas": 1, "maxReplicas": 8, "priority": "secondary"},
    "sockshop-orders-slo": {"targetDeployment": "orders", "sloLatency": 8.0, "minReplicas": 1, "maxReplicas": 6, "priority": "secondary"},
    "sockshop-user-slo": {"targetDeployment": "user", "sloLatency": 150.0, "minReplicas": 1, "maxReplicas": 6, "priority": "secondary"},
    "sockshop-payment-slo": {"targetDeployment": "payment", "sloLatency": 8.0, "minReplicas": 1, "maxReplicas": 4, "priority": "secondary"},
    "sockshop-shipping-slo": {"targetDeployment": "shipping", "sloLatency": 8.0, "minReplicas": 1, "maxReplicas": 4, "priority": "secondary"},
}

GATEWAY_SLO_BY_ROUTE = {
    "/cpu": 10.0,
    "/chain": 12.0,
    "/io": 100.0,
    "/fanout": 100.0,
    "/net": 100.0,
}

KUBECTL_TIMEOUT_SECONDS = 20
KUBECTL_RETRIES = 3
KUBECTL_RETRY_DELAY_SECONDS = 2


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


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
    for attempt in range(KUBECTL_RETRIES):
        try:
            output = subprocess.check_output(cmd, text=True, timeout=KUBECTL_TIMEOUT_SECONDS)
            return json.loads(output)
        except Exception:
            if attempt + 1 < KUBECTL_RETRIES:
                time.sleep(KUBECTL_RETRY_DELAY_SECONDS)
    return None


def kubectl_ok(args: list[str]) -> bool:
    cmd = kubectl_cmd(args)
    if not cmd:
        return False
    for attempt in range(KUBECTL_RETRIES):
        try:
            subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=KUBECTL_TIMEOUT_SECONDS)
            return True
        except Exception:
            if attempt + 1 < KUBECTL_RETRIES:
                time.sleep(KUBECTL_RETRY_DELAY_SECONDS)
    return False


def route_path_from_case_config(case_cfg: dict) -> str:
    routes = case_cfg.get("routes", {})
    phases = case_cfg.get("phases", [])
    if not isinstance(routes, dict) or not phases:
        return ""
    first_phase = phases[0] if isinstance(phases[0], dict) else {}
    route_mix = first_phase.get("route_mix", {})
    if not isinstance(route_mix, dict) or not route_mix:
        return ""
    route_names = list(route_mix.keys())
    if len(route_names) != 1:
        return ""
    route_cfg = routes.get(route_names[0], {})
    if not isinstance(route_cfg, dict):
        return ""
    return str(route_cfg.get("path", "")).strip()


def gateway_slo_for_case(case_cfg: dict) -> float:
    path = route_path_from_case_config(case_cfg)
    for prefix, slo_value in GATEWAY_SLO_BY_ROUTE.items():
        if path.startswith(prefix):
            return slo_value
    return 120.0


def replica_bounds_for_case(case_cfg: dict) -> tuple[int, int]:
    bounds = case_cfg.get("replica_bounds", {}) if isinstance(case_cfg, dict) else {}
    if not isinstance(bounds, dict):
        return (1, 5)
    min_replicas = int(bounds.get("minReplicas", 1) or 1)
    max_replicas = int(bounds.get("maxReplicas", 5) or 5)
    return (min_replicas, max(min_replicas, max_replicas))


def patch_serviceslo(namespace: str, name: str, spec_patch: dict) -> bool:
    payload = {"spec": spec_patch}
    return kubectl_ok(
        [
            "patch",
            "serviceslo",
            name,
            "-n",
            namespace,
            "--type=merge",
            "-p",
            json.dumps(payload, separators=(",", ":")),
        ]
    )


def get_serviceslo(namespace: str, name: str) -> dict:
    payload = kubectl_json(["get", "serviceslo", name, "-n", namespace, "-o", "json"])
    return payload if isinstance(payload, dict) else {}


def prepare_thrive_demo_slos(case_cfg: dict, namespace: str = "thrive-demo") -> dict:
    gateway_slo = gateway_slo_for_case(case_cfg)
    min_replicas, max_replicas = replica_bounds_for_case(case_cfg)
    gateway_patch = {
        "targetDeployment": "gateway",
        "sloLatency": gateway_slo,
        "minReplicas": min_replicas,
        "maxReplicas": max_replicas,
        "priority": "primary",
    }
    gateway_ok = patch_serviceslo(namespace, "gateway-slo", gateway_patch)

    downstream_results = {}
    downstream_ok = True
    for name, default_spec in DEFAULT_DOWNSTREAM_SLOS.items():
        spec_patch = dict(default_spec)
        spec_patch["minReplicas"] = min_replicas
        spec_patch["maxReplicas"] = max_replicas
        patched = patch_serviceslo(namespace, name, spec_patch)
        confirmed = get_serviceslo(namespace, name)
        spec = confirmed.get("spec", {}) if isinstance(confirmed, dict) else {}
        downstream_results[name] = {
            "patched": bool(patched),
            "targetDeployment": str(spec.get("targetDeployment", "")),
            "sloLatency": float(spec.get("sloLatency", 0.0) or 0.0),
            "minReplicas": int(spec.get("minReplicas", 0) or 0),
            "maxReplicas": int(spec.get("maxReplicas", 0) or 0),
            "priority": str(spec.get("priority", "")),
        }
        expected = spec_patch
        downstream_ok = downstream_ok and downstream_results[name]["targetDeployment"] == expected["targetDeployment"]
        downstream_ok = downstream_ok and downstream_results[name]["sloLatency"] == expected["sloLatency"]
        downstream_ok = downstream_ok and downstream_results[name]["minReplicas"] == expected["minReplicas"]
        downstream_ok = downstream_ok and downstream_results[name]["maxReplicas"] == expected["maxReplicas"]

    gateway_confirmed = get_serviceslo(namespace, "gateway-slo")
    gateway_spec = gateway_confirmed.get("spec", {}) if isinstance(gateway_confirmed, dict) else {}
    result = {
        "namespace": namespace,
        "route_path": route_path_from_case_config(case_cfg),
        "gateway_slo_latency": gateway_slo,
        "gateway_patch_applied": bool(gateway_ok),
        "replica_bounds": {
            "minReplicas": min_replicas,
            "maxReplicas": max_replicas,
        },
        "gateway_confirmed": {
            "targetDeployment": str(gateway_spec.get("targetDeployment", "")),
            "sloLatency": float(gateway_spec.get("sloLatency", 0.0) or 0.0),
            "minReplicas": int(gateway_spec.get("minReplicas", 0) or 0),
            "maxReplicas": int(gateway_spec.get("maxReplicas", 0) or 0),
            "priority": str(gateway_spec.get("priority", "")),
        },
        "downstream_slos": downstream_results,
    }
    result["gateway_confirmed_ok"] = (
        result["gateway_confirmed"]["targetDeployment"] == "gateway"
        and result["gateway_confirmed"]["sloLatency"] == gateway_slo
        and result["gateway_confirmed"]["minReplicas"] == min_replicas
        and result["gateway_confirmed"]["maxReplicas"] == max_replicas
    )
    result["downstream_confirmed_ok"] = downstream_ok
    result["ok"] = result["gateway_confirmed_ok"] and result["downstream_confirmed_ok"]
    return result


def prepare_sock_shop_slos(case_cfg: dict, namespace: str = "sock-shop") -> dict:
    min_replicas, max_replicas = replica_bounds_for_case(case_cfg)
    requested = case_cfg.get("service_slos", {}) if isinstance(case_cfg, dict) else {}
    if not isinstance(requested, dict) or not requested:
        requested = {}

    merged: dict[str, dict] = {}
    for name, default_spec in DEFAULT_SOCKSHOP_SLOS.items():
        spec_patch = dict(default_spec)
        spec_patch["minReplicas"] = min_replicas
        spec_patch["maxReplicas"] = max(max_replicas, int(default_spec.get("maxReplicas", max_replicas) or max_replicas))
        override = requested.get(name, {})
        if isinstance(override, dict):
            spec_patch.update({k: v for k, v in override.items() if k != "name"})
        merged[name] = spec_patch

    # Allow explicit extra SLO entries beyond the defaults.
    for name, override in requested.items():
        if name in merged or not isinstance(override, dict):
            continue
        spec_patch = dict(override)
        spec_patch.setdefault("minReplicas", min_replicas)
        spec_patch.setdefault("maxReplicas", max_replicas)
        merged[name] = spec_patch

    results = {}
    all_ok = True
    for name, spec_patch in merged.items():
        patched = patch_serviceslo(namespace, name, spec_patch)
        confirmed = get_serviceslo(namespace, name)
        spec = confirmed.get("spec", {}) if isinstance(confirmed, dict) else {}
        results[name] = {
            "patched": bool(patched),
            "targetDeployment": str(spec.get("targetDeployment", "")),
            "sloLatency": float(spec.get("sloLatency", 0.0) or 0.0),
            "minReplicas": int(spec.get("minReplicas", 0) or 0),
            "maxReplicas": int(spec.get("maxReplicas", 0) or 0),
            "priority": str(spec.get("priority", "")),
        }
        all_ok = all_ok and results[name]["targetDeployment"] == str(spec_patch.get("targetDeployment", ""))
        all_ok = all_ok and results[name]["sloLatency"] == float(spec_patch.get("sloLatency", 0.0) or 0.0)
        all_ok = all_ok and results[name]["minReplicas"] == int(spec_patch.get("minReplicas", 0) or 0)
        all_ok = all_ok and results[name]["maxReplicas"] == int(spec_patch.get("maxReplicas", 0) or 0)

    return {
        "namespace": namespace,
        "replica_bounds": {
            "minReplicas": min_replicas,
            "maxReplicas": max_replicas,
        },
        "service_slos": results,
        "ok": all_ok,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Patch thrive-demo gateway/downstream ServiceSLOs for one test case.")
    parser.add_argument("--case-config", required=True, type=Path, help="Case config JSON")
    parser.add_argument("--namespace", default="thrive-demo", help="Namespace containing the demo ServiceSLOs")
    args = parser.parse_args()

    case_cfg = load_json(args.case_config)
    result = prepare_thrive_demo_slos(case_cfg, namespace=args.namespace)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
