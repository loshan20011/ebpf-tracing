#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

import yaml
from kubernetes import client, config


def load_expected(file_path: Path, namespace: str):
    docs = list(yaml.safe_load_all(file_path.read_text()))
    expected = {}
    for doc in docs:
        if not doc or doc.get("kind") != "ServiceSLO":
            continue
        meta = doc.get("metadata", {})
        spec = doc.get("spec", {})
        ns = meta.get("namespace", namespace)
        if ns != namespace:
            continue
        name = meta.get("name")
        target = spec.get("targetDeployment")
        if not name or not target:
            continue
        expected[name] = {
            "targetDeployment": str(target),
            "sloLatency": float(spec.get("sloLatency", 0.0)),
            "minReplicas": int(spec.get("minReplicas", 1)),
            "maxReplicas": int(spec.get("maxReplicas", 1)),
        }
    return expected


def load_live(namespace: str):
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()

    api = client.CustomObjectsApi()
    raw = api.list_namespaced_custom_object(
        group="autoscaling.fyp.io",
        version="v1alpha1",
        namespace=namespace,
        plural="serviceslos",
    )
    live = {}
    for item in raw.get("items", []):
        meta = item.get("metadata", {})
        spec = item.get("spec", {})
        name = meta.get("name")
        if not name:
            continue
        live[name] = {
            "targetDeployment": str(spec.get("targetDeployment", "")),
            "sloLatency": float(spec.get("sloLatency", 0.0)),
            "minReplicas": int(spec.get("minReplicas", 1)),
            "maxReplicas": int(spec.get("maxReplicas", 1)),
        }
    return live


def main():
    p = argparse.ArgumentParser(description="Validate live ServiceSLOs against YAML")
    p.add_argument("--file", required=True, help="Path to SLO YAML (multi-doc supported)")
    p.add_argument("--namespace", required=True, help="Target namespace")
    args = p.parse_args()

    f = Path(args.file)
    if not f.exists():
        print(f"[error] file not found: {f}")
        return 2

    expected = load_expected(f, args.namespace)
    if not expected:
        print(f"[error] no ServiceSLO docs found for namespace={args.namespace} in {f}")
        return 2

    live = load_live(args.namespace)

    errors = []
    for name, exp in expected.items():
        got = live.get(name)
        if not got:
            errors.append(f"missing live ServiceSLO: {name}")
            continue
        for key in ("targetDeployment", "sloLatency", "minReplicas", "maxReplicas"):
            if got[key] != exp[key]:
                errors.append(
                    f"{name} mismatch {key}: expected={exp[key]} live={got[key]}"
                )

    extra = sorted(set(live.keys()) - set(expected.keys()))
    if extra:
        errors.append(f"unexpected live ServiceSLOs: {', '.join(extra)}")

    if errors:
        print("[fail] ServiceSLO validation failed:")
        for e in errors:
            print(f" - {e}")
        return 1

    print(f"[ok] ServiceSLOs match source file in namespace={args.namespace}")
    for name in sorted(expected.keys()):
        item = expected[name]
        print(
            f" - {name}: target={item['targetDeployment']} slo={item['sloLatency']} min={item['minReplicas']} max={item['maxReplicas']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
