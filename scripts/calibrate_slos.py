#!/usr/bin/env python3
import argparse
import math
import subprocess
import sys
import time
from pathlib import Path

import requests
import yaml


def run(cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


def discover_aggregator_url(control_ns: str):
    lb = run([
        "kubectl", "get", "svc", "aggregator", "-n", control_ns,
        "-o", "jsonpath={.status.loadBalancer.ingress[0].hostname}"
    ]).stdout.strip()
    if lb:
        return f"http://{lb}:8000"

    node_port = run([
        "kubectl", "get", "svc", "aggregator", "-n", control_ns,
        "-o", "jsonpath={.spec.ports[0].nodePort}"
    ]).stdout.strip()
    if node_port:
        ext_ip = run([
            "kubectl", "get", "nodes", "-o",
            "jsonpath={.items[0].status.addresses[?(@.type==\"ExternalIP\")].address}"
        ]).stdout.strip()
        if ext_ip:
            return f"http://{ext_ip}:{node_port}"
        int_ip = run([
            "kubectl", "get", "nodes", "-o",
            "jsonpath={.items[0].status.addresses[?(@.type==\"InternalIP\")].address}"
        ]).stdout.strip()
        if int_ip:
            return f"http://{int_ip}:{node_port}"

    return ""


def read_slos(path: Path, namespace: str):
    docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    out = []
    for doc in docs:
        if not doc or doc.get("kind") != "ServiceSLO":
            continue
        md = doc.get("metadata", {})
        spec = doc.get("spec", {})
        ns = md.get("namespace", namespace)
        if ns != namespace:
            continue
        if not spec.get("targetDeployment"):
            continue
        out.append(doc)
    return out


def fetch_metrics(aggregator_url: str):
    resp = requests.get(f"{aggregator_url}/api/graph?include_infra=1", timeout=5)
    resp.raise_for_status()
    return resp.json().get("metrics", {})


def main():
    p = argparse.ArgumentParser(description="Calibrate ServiceSLOs from baseline run")
    p.add_argument("--app-namespace", default="sock-shop")
    p.add_argument("--control-namespace", default="thrive-scale")
    p.add_argument("--slo-file", default="deploy/03-evaluation/sockshop-slos.yaml")
    p.add_argument("--output", default="deploy/03-evaluation/sockshop-slos.calibrated.yaml")
    p.add_argument("--frontend-url", required=True)
    p.add_argument("--mix-file", default="deploy/03-evaluation/workloads/sockshop-ew-mix.yaml")
    p.add_argument("--duration", type=int, default=120)
    p.add_argument("--warmup-seconds", type=int, default=20)
    p.add_argument("--factor", type=float, default=1.15)
    p.add_argument("--aggregator-url", default="")
    args = p.parse_args()

    slo_path = Path(args.slo_file)
    if not slo_path.exists():
        raise SystemExit(f"SLO file not found: {slo_path}")

    docs = read_slos(slo_path, args.app_namespace)
    if not docs:
        raise SystemExit(f"No ServiceSLO docs found in {slo_path} for namespace={args.app_namespace}")

    # Disable controller during calibration baseline
    subprocess.run(["kubectl", "scale", "deploy/custom-autoscaler", "-n", args.control_namespace, "--replicas=0"], check=False)

    calib_csv = "results/results_sockshop_calibration_baseline.csv"
    cmd = [
        sys.executable,
        "src/load-generator/eval_harness.py",
        "--url", args.frontend_url,
        "--deployment", "front-end",
        "--namespace", args.app_namespace,
        "--profile", "sockshop",
        "--mix-file", args.mix_file,
        "--duration", str(args.duration),
        "--warmup-seconds", str(args.warmup_seconds),
        "--sample-interval", "2",
        "--timeout", "8",
        "--csv", calib_csv,
    ]
    print("[calibrate] Running baseline harness for SLO calibration...")
    subprocess.run(cmd, check=True)

    aggregator_url = args.aggregator_url.strip() or discover_aggregator_url(args.control_namespace)
    if not aggregator_url:
        raise SystemExit("Could not discover aggregator URL; provide --aggregator-url")

    # Give aggregator one scrape cycle after baseline.
    time.sleep(3)
    metrics = fetch_metrics(aggregator_url)

    for doc in docs:
        target = doc["spec"]["targetDeployment"]
        current = float(doc["spec"].get("sloLatency", 100.0))
        p90 = float(metrics.get(target, {}).get("p90_latency", 0.0))
        # Guardrails: keep original SLO when baseline metric is missing/noisy.
        # Very high p90 here usually means sparse/no traffic for that service in baseline.
        if 1.0 <= p90 <= 2000.0:
            calibrated = max(1, int(math.ceil(p90 * args.factor)))
        else:
            calibrated = int(current)
        doc["spec"]["sloLatency"] = calibrated
        doc.setdefault("metadata", {})["namespace"] = args.app_namespace
        print(f"[calibrate] {target}: p90={p90:.2f}ms old={current:.2f} -> slo={calibrated}ms")

    out_path = Path(args.output)
    out_path.write_text("---\n".join(yaml.safe_dump(d, sort_keys=False) for d in docs), encoding="utf-8")
    print(f"[ok] wrote calibrated SLO file: {out_path}")


if __name__ == "__main__":
    main()
