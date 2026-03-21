#!/usr/bin/env python3
import argparse
import json
import sys
from typing import List

import requests
from kubernetes import client, config


def load_clients():
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()
    return client.AppsV1Api(), client.CustomObjectsApi()


def deployment_state(apps_api, namespace: str, name: str):
    dep = apps_api.read_namespaced_deployment(name=name, namespace=namespace)
    desired = int(dep.spec.replicas or 0)
    ready = int(dep.status.ready_replicas or 0)
    updated = int(dep.status.updated_replicas or 0)
    available = int(dep.status.available_replicas or 0)
    return {
        "desired": desired,
        "ready": ready,
        "updated": updated,
        "available": available,
        "rollout_lag": max(0, desired - ready),
    }


def hpa_names(custom_api, namespace: str) -> List[str]:
    try:
        raw = custom_api.list_namespaced_custom_object(
            group="autoscaling",
            version="v2",
            namespace=namespace,
            plural="horizontalpodautoscalers",
        )
    except Exception:
        return []
    return [str(i.get("metadata", {}).get("name", "")) for i in raw.get("items", []) if i.get("metadata", {}).get("name")]


def service_slo_targets(custom_api, namespace: str) -> List[str]:
    try:
        raw = custom_api.list_namespaced_custom_object(
            group="autoscaling.fyp.io",
            version="v1alpha1",
            namespace=namespace,
            plural="serviceslos",
        )
    except Exception:
        return []
    out = []
    for item in raw.get("items", []):
        target = str(item.get("spec", {}).get("targetDeployment", "")).strip()
        if target:
            out.append(target)
    return out


def aggregator_health(aggregator_url: str):
    out = {"reachable": False, "ready": False, "truth_present": False, "services": []}
    try:
        r = requests.get(f"{aggregator_url.rstrip('/')}/readyz", timeout=2)
        out["ready"] = r.status_code == 200
    except Exception:
        out["ready"] = False
    try:
        r = requests.get(f"{aggregator_url.rstrip('/')}/api/graph", timeout=2)
        r.raise_for_status()
        payload = r.json()
        metrics = payload.get("metrics", {})
        out["reachable"] = True
        out["services"] = sorted(metrics.keys())
        out["truth_present"] = any(
            float((m or {}).get("truth_rps", 0.0) or 0.0) > 0.0
            or int((m or {}).get("truth_req_count", 0) or 0) > 0
            or float((m or {}).get("truth_p90_latency_ms", 0.0) or 0.0) > 0.0
            or bool((m or {}).get("truth_fresh", False))
            for m in metrics.values()
            if isinstance(m, dict)
        )
    except Exception:
        out["reachable"] = False
    return out


def main():
    p = argparse.ArgumentParser(description="Verify benchmark baseline before running autoscaling experiments")
    p.add_argument("--app-namespace", required=True)
    p.add_argument("--control-namespace", required=True)
    p.add_argument("--aggregator-url", required=True)
    p.add_argument("--services", nargs="+", required=True)
    p.add_argument("--expected-replicas", type=int, default=1)
    p.add_argument("--mode", choices=["idle", "hpa", "thrivescale"], required=True)
    p.add_argument("--require-truth", action="store_true")
    args = p.parse_args()

    apps_api, custom_api = load_clients()
    failures = []
    report = {
        "mode": args.mode,
        "app_namespace": args.app_namespace,
        "control_namespace": args.control_namespace,
        "services": {},
    }

    for svc in args.services:
        try:
            state = deployment_state(apps_api, args.app_namespace, svc)
            report["services"][svc] = state
            if state["desired"] != args.expected_replicas or state["ready"] != args.expected_replicas:
                failures.append(f"deployment {svc} not at baseline replicas ({state['desired']}/{state['ready']})")
            if state["rollout_lag"] > 0 or state["updated"] != state["desired"] or state["available"] != state["desired"]:
                failures.append(f"deployment {svc} still rolling out")
        except Exception as exc:
            failures.append(f"deployment {svc} unreadable: {exc}")

    report["hpas"] = hpa_names(custom_api, args.app_namespace)
    if args.mode == "hpa":
        if not report["hpas"]:
            failures.append("expected HPA objects but found none")
    else:
        if report["hpas"]:
            failures.append(f"unexpected HPA objects present: {','.join(report['hpas'])}")

    report["slos"] = sorted(service_slo_targets(custom_api, args.app_namespace))
    missing_slos = sorted(set(args.services) - set(report["slos"]))
    if missing_slos:
        failures.append(f"missing ServiceSLO targets: {','.join(missing_slos)}")

    try:
        autoscaler = deployment_state(apps_api, args.control_namespace, "custom-autoscaler")
        report["custom_autoscaler"] = autoscaler
        if args.mode == "thrivescale":
            if autoscaler["desired"] < 1 or autoscaler["ready"] < 1:
                failures.append("custom-autoscaler is not running in ThriveScale mode")
        else:
            if autoscaler["desired"] != 0:
                failures.append("custom-autoscaler must be scaled to 0 outside ThriveScale mode")
    except Exception as exc:
        report["custom_autoscaler"] = {"error": str(exc)}
        if args.mode == "thrivescale":
            failures.append(f"custom-autoscaler unavailable: {exc}")

    report["aggregator"] = aggregator_health(args.aggregator_url)
    if not report["aggregator"]["reachable"]:
        failures.append("aggregator /api/graph unreachable")
    if not report["aggregator"]["ready"]:
        failures.append("aggregator /readyz not ready")
    if args.require_truth and not report["aggregator"]["truth_present"]:
        failures.append("truth ingestion not yet visible in aggregator metrics")

    report["ok"] = not failures
    report["failures"] = failures
    print(json.dumps(report, indent=2, sort_keys=True))
    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
