#!/usr/bin/env python3
import argparse
import csv
import json
import time
from datetime import datetime, timezone

import requests
from kubernetes import client, config


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_clients():
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()
    return client.AppsV1Api(), client.CoreV1Api()


def read_replicas(apps_api, core_api, namespace: str, deployment: str):
    try:
        dep = apps_api.read_namespaced_deployment(name=deployment, namespace=namespace)
    except Exception:
        return 0, 0

    spec_replicas = int(dep.spec.replicas or 0)
    ready = int(dep.status.ready_replicas or 0)
    return spec_replicas, ready


def metric_latency(metrics, svc: str):
    m = metrics.get(svc, {}) if isinstance(metrics, dict) else {}
    p90 = float(m.get("p90_latency", m.get("latency", 0.0)) or 0.0)
    avg = float(m.get("latency", m.get("p90_latency", 0.0)) or 0.0)
    return p90, avg


def all_service_latencies(metrics):
    p90_map = {}
    avg_map = {}
    if not isinstance(metrics, dict):
        return p90_map, avg_map
    for svc, v in metrics.items():
        if not isinstance(v, dict):
            continue
        p90_map[svc] = float(v.get("p90_latency", v.get("latency", 0.0)) or 0.0)
        avg_map[svc] = float(v.get("latency", v.get("p90_latency", 0.0)) or 0.0)
    return p90_map, avg_map


def all_service_states(metrics):
    state_map = {}
    if not isinstance(metrics, dict):
        return state_map
    for svc, v in metrics.items():
        if not isinstance(v, dict):
            continue
        state_map[svc] = {
            "active_short": bool(v.get("active_short", False)),
            "active_long": bool(v.get("active_long", False)),
            "evaluable_for_slo": bool(v.get("evaluable_for_slo", False)),
            "latency_fresh": bool(v.get("latency_fresh", False)),
            "truth_fresh": bool(v.get("truth_fresh", False)),
            "topology_fresh": bool(v.get("topology_fresh", False)),
            "evidence_confidence": float(v.get("evidence_confidence", 0.0) or 0.0),
            "rps": float(v.get("rps", 0.0) or 0.0),
            "rps_long": float(v.get("rps_long", 0.0) or 0.0),
            "truth_rps": float(v.get("truth_rps", 0.0) or 0.0),
            "truth_rps_long": float(v.get("truth_rps_long", 0.0) or 0.0),
            "count": int(v.get("count", 0) or 0),
            "count_long": int(v.get("count_long", 0) or 0),
            "truth_req_count": int(v.get("truth_req_count", 0) or 0),
            "truth_req_count_long": int(v.get("truth_req_count_long", 0) or 0),
            "truth_5xx_rate": float(v.get("truth_5xx_rate", 0.0) or 0.0),
            "truth_timeout_rate": float(v.get("truth_timeout_rate", 0.0) or 0.0),
        }
    return state_map


def read_all_deployment_replicas(apps_api, namespace: str):
    try:
        deps = apps_api.list_namespaced_deployment(namespace=namespace)
    except Exception:
        return {}, {}, 0, 0

    spec_map = {}
    ready_map = {}
    for dep in deps.items:
        name = dep.metadata.name
        spec_map[name] = int(dep.spec.replicas or 0)
        ready_map[name] = int(dep.status.ready_replicas or 0)
    return spec_map, ready_map, sum(spec_map.values()), sum(ready_map.values())


def fetch_graph_metrics(aggregator_url: str):
    try:
        resp = requests.get(f"{aggregator_url.rstrip('/')}/api/graph", timeout=2)
        resp.raise_for_status()
        return resp.json().get("metrics", {})
    except Exception:
        return {}


def fetch_traces(aggregator_url: str):
    try:
        resp = requests.get(f"{aggregator_url.rstrip('/')}/api/traces", timeout=2)
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        return []
    traces = payload.get("traces", [])
    return traces if isinstance(traces, list) else []


def discover_slo_latency_ms(namespace: str, deployment: str):
    try:
        custom = client.CustomObjectsApi()
        raw = custom.list_namespaced_custom_object(
            group="autoscaling.fyp.io",
            version="v1alpha1",
            namespace=namespace,
            plural="serviceslos",
        )
    except Exception:
        return 0.0
    for item in raw.get("items", []):
        spec = item.get("spec", {})
        if str(spec.get("targetDeployment", "")).strip() == deployment:
            try:
                return float(spec.get("sloLatency", 0.0))
            except Exception:
                return 0.0
    return 0.0


def parse_scale_target(action: str):
    raw = str(action or "")
    if not raw.startswith("scale_to_"):
        return None
    try:
        return int(raw.split("scale_to_", 1)[1])
    except Exception:
        return None


def main():
    p = argparse.ArgumentParser(description="Sample cluster latency + replica metrics into eval CSV")
    p.add_argument("--aggregator-url", required=True)
    p.add_argument("--namespace", required=True)
    p.add_argument("--deployment", required=True)
    p.add_argument("--duration", type=float, default=180)
    p.add_argument("--sample-interval", type=float, default=2)
    p.add_argument("--warmup-seconds", type=float, default=20)
    p.add_argument("--target-rps", type=float, default=0.0)
    p.add_argument("--slo-latency-ms", type=float, default=0.0)
    p.add_argument("--control-target", default="")
    p.add_argument("--breach-csv", default="")
    p.add_argument("--csv", required=True)
    args = p.parse_args()

    apps_api, core_api = load_clients()
    start = time.time()
    start_unix_ms = int(start * 1000)
    trace_seen = set()
    trace_order = []
    max_trace_cache = 4000
    control_target = args.control_target.strip() or args.deployment
    slo_latency_ms = float(args.slo_latency_ms)
    if slo_latency_ms <= 0:
        slo_latency_ms = discover_slo_latency_ms(args.namespace, args.deployment)
    breach_csv = args.breach_csv.strip() or (args.csv[:-4] + ".breaches.csv" if args.csv.endswith(".csv") else args.csv + ".breaches.csv")
    active_breach = None
    breach_id = 0

    with open(args.csv, "w", newline="", encoding="utf-8") as f:
        with open(breach_csv, "w", newline="", encoding="utf-8") as b:
            bw = csv.writer(b)
            bw.writerow(
                [
                    "breach_id",
                    "first_slo_breach_s",
                    "first_controller_trigger_s",
                    "first_scale_decision_s",
                    "first_pod_ready_s",
                    "recovery_s",
                    "time_to_recovery_s",
                    "scale_target",
                    "breach_ready_replicas",
                ]
            )
            w = csv.writer(f)
            w.writerow(
                [
                    "timestamp_utc",
                    "elapsed_s",
                    "target_rps",
                    "latency_p90_ms",
                    "latency_avg_ms",
                    "requests_sent",
                    "requests_ok",
                    "requests_err",
                    "route_group_counts_json",
                    "in_score_window",
                    "deployment",
                    "pod_spec_replicas",
                    "pod_ready_replicas",
                    "service_p90_json",
                    "service_avg_json",
                    "service_state_json",
                    "all_deployment_spec_replicas_json",
                    "all_deployment_ready_replicas_json",
                    "all_spec_replicas",
                    "all_ready_replicas",
                    "slo_latency_ms",
                    "active_breach_id",
                ]
            )

            while True:
                now = time.time()
                elapsed = now - start
                if elapsed > args.duration:
                    break

                metrics = fetch_graph_metrics(args.aggregator_url)
                p90, avg = metric_latency(metrics, args.deployment)
                service_p90, service_avg = all_service_latencies(metrics)
                service_state = all_service_states(metrics)
                spec, ready = read_replicas(apps_api, core_api, args.namespace, args.deployment)
                all_spec_map, all_ready_map, all_spec, all_ready = read_all_deployment_replicas(
                    apps_api, args.namespace
                )
                in_score = 1 if elapsed >= args.warmup_seconds else 0
                breach_now = in_score == 1 and slo_latency_ms > 0 and p90 > slo_latency_ms
                if breach_now and active_breach is None:
                    breach_id += 1
                    active_breach = {
                        "breach_id": breach_id,
                        "first_slo_breach_s": round(elapsed, 3),
                        "breach_unix_ms": start_unix_ms + int(elapsed * 1000.0),
                        "first_controller_trigger_s": None,
                        "first_scale_decision_s": None,
                        "first_pod_ready_s": None,
                        "recovery_s": None,
                        "time_to_recovery_s": None,
                        "scale_target": None,
                        "breach_ready_replicas": int(ready),
                    }

                if active_breach is not None:
                    traces = fetch_traces(args.aggregator_url)
                    traces.sort(key=lambda t: int(t.get("ts_unix_ms", 0)))
                    for tr in traces:
                        ts_ms = int(tr.get("ts_unix_ms", 0) or 0)
                        root = str(tr.get("root", ""))
                        node = str(tr.get("node", ""))
                        action = str(tr.get("action", ""))
                        tid = (ts_ms, root, node, action)
                        if tid in trace_seen:
                            continue
                        trace_seen.add(tid)
                        trace_order.append(tid)
                        if len(trace_order) > max_trace_cache:
                            old = trace_order.pop(0)
                            trace_seen.discard(old)
                        if ts_ms < int(active_breach["breach_unix_ms"]):
                            continue
                        if root != control_target:
                            continue
                        rel_s = round((ts_ms - start_unix_ms) / 1000.0, 3)
                        if active_breach["first_controller_trigger_s"] is None:
                            active_breach["first_controller_trigger_s"] = rel_s
                        if action.startswith("scale_to_") and active_breach["first_scale_decision_s"] is None:
                            active_breach["first_scale_decision_s"] = rel_s
                            active_breach["scale_target"] = parse_scale_target(action)

                    if active_breach["first_pod_ready_s"] is None:
                        ready_threshold = int(active_breach["breach_ready_replicas"]) + 1
                        scale_target = active_breach.get("scale_target")
                        if isinstance(scale_target, int) and scale_target > int(active_breach["breach_ready_replicas"]):
                            ready_threshold = scale_target
                        if int(ready) >= ready_threshold:
                            active_breach["first_pod_ready_s"] = round(elapsed, 3)

                if active_breach is not None and not breach_now:
                    active_breach["recovery_s"] = round(elapsed, 3)
                    active_breach["time_to_recovery_s"] = round(
                        float(active_breach["recovery_s"]) - float(active_breach["first_slo_breach_s"]),
                        3,
                    )
                    bw.writerow(
                        [
                            active_breach["breach_id"],
                            active_breach["first_slo_breach_s"],
                            active_breach["first_controller_trigger_s"],
                            active_breach["first_scale_decision_s"],
                            active_breach["first_pod_ready_s"],
                            active_breach["recovery_s"],
                            active_breach["time_to_recovery_s"],
                            active_breach["scale_target"],
                            active_breach["breach_ready_replicas"],
                        ]
                    )
                    b.flush()
                    active_breach = None

                w.writerow(
                    [
                        now_iso(),
                        f"{elapsed:.3f}",
                        f"{args.target_rps:.2f}",
                        f"{p90:.3f}",
                        f"{avg:.3f}",
                        0,
                        0,
                        0,
                        json.dumps({}, separators=(",", ":")),
                        in_score,
                        args.deployment,
                        spec,
                        ready,
                        json.dumps(service_p90, separators=(",", ":"), sort_keys=True),
                        json.dumps(service_avg, separators=(",", ":"), sort_keys=True),
                        json.dumps(service_state, separators=(",", ":"), sort_keys=True),
                        json.dumps(all_spec_map, separators=(",", ":"), sort_keys=True),
                        json.dumps(all_ready_map, separators=(",", ":"), sort_keys=True),
                        all_spec,
                        all_ready,
                        f"{slo_latency_ms:.3f}",
                        active_breach["breach_id"] if active_breach else "",
                    ]
                )
                f.flush()
                time.sleep(max(0.05, args.sample_interval))

            if active_breach is not None:
                bw.writerow(
                    [
                        active_breach["breach_id"],
                        active_breach["first_slo_breach_s"],
                        active_breach["first_controller_trigger_s"],
                        active_breach["first_scale_decision_s"],
                        active_breach["first_pod_ready_s"],
                        active_breach["recovery_s"],
                        active_breach["time_to_recovery_s"],
                        active_breach["scale_target"],
                        active_breach["breach_ready_replicas"],
                    ]
                )
                b.flush()


if __name__ == "__main__":
    main()
