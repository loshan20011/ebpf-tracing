#!/usr/bin/env python3
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


DEFAULT_DOWNSTREAM_SLOS = {
    "cpu-slo": {"targetDeployment": "svc-cpu", "sloLatency": 10.0, "minReplicas": 1, "maxReplicas": 5, "priority": "secondary"},
    "chain-slo": {"targetDeployment": "svc-chain", "sloLatency": 12.0, "minReplicas": 1, "maxReplicas": 5, "priority": "secondary"},
    "io-slo": {"targetDeployment": "svc-io", "sloLatency": 100.0, "minReplicas": 1, "maxReplicas": 5, "priority": "secondary"},
    "fanout-slo": {"targetDeployment": "svc-fanout", "sloLatency": 100.0, "minReplicas": 1, "maxReplicas": 5, "priority": "secondary"},
}

GATEWAY_SLO_BY_ROUTE = {
    "/cpu": 10.0,
    "/chain": 12.0,
    "/io": 100.0,
    "/fanout": 100.0,
    "/net": 100.0,
}


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
    gateway_patch = {
        "targetDeployment": "gateway",
        "sloLatency": gateway_slo,
        "minReplicas": 1,
        "maxReplicas": 5,
        "priority": "primary",
    }
    gateway_ok = patch_serviceslo(namespace, "gateway-slo", gateway_patch)

    downstream_results = {}
    downstream_ok = True
    for name, spec_patch in DEFAULT_DOWNSTREAM_SLOS.items():
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
        expected = DEFAULT_DOWNSTREAM_SLOS[name]
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
        and result["gateway_confirmed"]["minReplicas"] == 1
        and result["gateway_confirmed"]["maxReplicas"] == 5
    )
    result["downstream_confirmed_ok"] = downstream_ok
    result["ok"] = result["gateway_confirmed_ok"] and result["downstream_confirmed_ok"]
    return result


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
