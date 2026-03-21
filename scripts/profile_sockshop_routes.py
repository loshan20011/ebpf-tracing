#!/usr/bin/env python3
import argparse
import csv
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests
import yaml


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROUTE_CATALOG = ROOT / "deploy/03-evaluation/workloads/worldcup98-day75-peak.yaml"
DEFAULT_OUTPUT_DIR = ROOT / "results/route-profiles"
DEFAULT_FRONTEND_SLO_MS = 41.0
DEFAULT_RUNQ_STRONG_MS = 6.0
DEFAULT_RUNQ_SOFT_MS = 3.0


@dataclass
class RouteCandidate:
    name: str
    label: str
    path: str
    target_deployment: str


def now_utc_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def run(cmd: List[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def canonical_route(route: str) -> str:
    raw = str(route or "").strip().lower()
    if not raw:
        return "_all"
    return raw[:180].replace(" ", "_")


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def discover_aggregator_url(control_ns: str) -> str:
    lb = run(
        [
            "kubectl",
            "get",
            "svc",
            "aggregator",
            "-n",
            control_ns,
            "-o",
            "jsonpath={.status.loadBalancer.ingress[0].hostname}",
        ],
        check=False,
    ).stdout.strip()
    if lb:
        return f"http://{lb}:8000"

    node_port = run(
        [
            "kubectl",
            "get",
            "svc",
            "aggregator",
            "-n",
            control_ns,
            "-o",
            "jsonpath={.spec.ports[0].nodePort}",
        ],
        check=False,
    ).stdout.strip()
    if node_port:
        ext_ip = run(
            [
                "kubectl",
                "get",
                "nodes",
                "-o",
                'jsonpath={.items[0].status.addresses[?(@.type=="ExternalIP")].address}',
            ],
            check=False,
        ).stdout.strip()
        if ext_ip:
            return f"http://{ext_ip}:{node_port}"
        int_ip = run(
            [
                "kubectl",
                "get",
                "nodes",
                "-o",
                'jsonpath={.items[0].status.addresses[?(@.type=="InternalIP")].address}',
            ],
            check=False,
        ).stdout.strip()
        if int_ip:
            return f"http://{int_ip}:{node_port}"
    return ""


def fetch_json(url: str, timeout: float = 5.0, headers: Optional[Dict[str, str]] = None) -> dict:
    resp = requests.get(url, timeout=timeout, headers=headers or {})
    resp.raise_for_status()
    payload = resp.json()
    return payload if isinstance(payload, dict) else {}


def load_route_catalog(path: Path) -> List[dict]:
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = raw.get("routes", [])
    return rows if isinstance(rows, list) else []


def infer_target_from_path(path: str) -> str:
    norm = str(path or "").strip().lower()
    if norm in {"/", "/category.html"}:
        return "front-end"
    if norm.startswith("/catalogue") or norm.startswith("/detail.html"):
        return "catalogue"
    if norm.startswith("/basket") or norm.startswith("/cart"):
        return "carts"
    if norm.startswith("/customer-orders"):
        return "orders"
    return "front-end"


def merge_sockshop_routes(control_state: dict, catalog_rows: List[dict]) -> List[RouteCandidate]:
    control_routes = control_state.get("trafficRoutes", [])
    by_path: Dict[str, RouteCandidate] = {}

    for row in control_routes:
        path = str(row.get("path", "")).strip()
        if not path:
            continue
        candidate = RouteCandidate(
            name=str(row.get("name") or path.strip("/").replace("/", "_") or "root"),
            label=str(row.get("label") or row.get("name") or path),
            path=path,
            target_deployment=str(row.get("targetDeployment") or infer_target_from_path(path)),
        )
        if candidate.name == "cart_view" and candidate.path == "/cart.html":
            candidate.path = "/basket.html"
        by_path[candidate.path] = candidate

    for row in catalog_rows:
        path = str(row.get("path", "")).strip()
        if not path:
            continue
        candidate = RouteCandidate(
            name=str(row.get("name") or path.strip("/").replace("/", "_") or "root"),
            label=str(row.get("label") or row.get("name") or path),
            path=path,
            target_deployment=infer_target_from_path(path),
        )
        by_path[path] = candidate

    ordered = sorted(by_path.values(), key=lambda r: (r.target_deployment, r.path))
    return ordered


def build_single_route_mix(route: RouteCandidate, levels: List[float], phase_seconds: int) -> dict:
    start_s = 0
    phases = []
    for rps in levels:
        phases.append({"start_s": start_s, "end_s": start_s + phase_seconds, "target_rps": float(rps)})
        start_s += phase_seconds
    return {
        "metadata": {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "purpose": "Sock Shop route CPU-bound profiling",
            "route": route.name,
            "path": route.path,
            "target_deployment": route.target_deployment,
        },
        "phases": phases,
        "routes": [
            {
                "name": route.name,
                "group": route.target_deployment or "route-profile",
                "method": "GET",
                "path": route.path,
                "weight": 100,
            }
        ],
    }


def kube_scale(namespace: str, name: str, replicas: int, kind: str = "deployment") -> None:
    run(["kubectl", "scale", f"{kind}/{name}", "-n", namespace, f"--replicas={int(replicas)}"])


def kube_rollout(namespace: str, name: str, kind: str = "deployment", timeout: str = "180s") -> None:
    run(["kubectl", "rollout", "status", "-n", namespace, f"{kind}/{name}", f"--timeout={timeout}"])


def hpa_names(namespace: str) -> List[str]:
    raw = run(
        [
            "kubectl",
            "get",
            "hpa",
            "-n",
            namespace,
            "-o",
            "jsonpath={range .items[*]}{.metadata.name}{\"\\n\"}{end}",
        ],
        check=False,
    ).stdout
    return [line.strip() for line in raw.splitlines() if line.strip()]


def autoscaler_replicas(control_ns: str) -> int:
    raw = run(
        [
            "kubectl",
            "get",
            "deploy",
            "custom-autoscaler",
            "-n",
            control_ns,
            "-o",
            "jsonpath={.spec.replicas}",
        ],
        check=False,
    ).stdout.strip()
    try:
        return int(raw or 0)
    except Exception:
        return 0


def reset_aggregator(aggregator_url: str, token: str = "") -> None:
    headers = {"X-Control-Token": token} if token else {}
    resp = requests.get(f"{aggregator_url.rstrip('/')}/api/reset", timeout=5, headers=headers)
    resp.raise_for_status()


def fetch_graph(aggregator_url: str) -> dict:
    return fetch_json(f"{aggregator_url.rstrip('/')}/api/graph", timeout=2.5)


def extract_snapshot(elapsed_s: float, graph: dict, route: RouteCandidate, frontend_slo_ms: float) -> dict:
    metrics = graph.get("metrics", {}) if isinstance(graph, dict) else {}
    front = metrics.get("front-end", {}) if isinstance(metrics, dict) else {}
    target = metrics.get(route.target_deployment, {}) if isinstance(metrics, dict) else {}
    route_truth = {}
    truth_routes = front.get("truth_routes", {}) if isinstance(front, dict) else {}
    if isinstance(truth_routes, dict):
        route_truth = truth_routes.get(canonical_route(route.path), {}) or {}

    route_p90 = safe_float(route_truth.get("p90_latency_ms", 0.0))
    route_rps = safe_float(route_truth.get("rps", 0.0))
    route_error_rate = (
        safe_float(route_truth.get("timeout_rate", 0.0))
        + safe_float(route_truth.get("connect_refused_rate", 0.0))
        + safe_float(route_truth.get("error_5xx_rate", 0.0))
    )

    target_p90 = safe_float(target.get("p90_latency", target.get("latency", 0.0)))
    target_runq = safe_float(target.get("avg_runq_latency", 0.0))
    target_exclusive = safe_float(target.get("exclusive_delay", 0.0))
    target_rps = safe_float(target.get("rps", 0.0))
    target_local_share = target_exclusive / target_p90 if target_p90 > 0 else 0.0
    frontend_truth_p90 = safe_float(front.get("truth_p90_latency_ms", 0.0))
    frontend_exclusive = safe_float(front.get("exclusive_delay", 0.0))
    frontend_runq = safe_float(front.get("avg_runq_latency", 0.0))
    frontend_qos_ratio = route_p90 / frontend_slo_ms if frontend_slo_ms > 0 else 0.0

    return {
        "elapsed_s": round(elapsed_s, 3),
        "route_p90_ms": round(route_p90, 3),
        "route_rps": round(route_rps, 3),
        "route_error_rate": round(route_error_rate, 6),
        "frontend_truth_p90_ms": round(frontend_truth_p90, 3),
        "frontend_exclusive_ms": round(frontend_exclusive, 3),
        "frontend_runq_ms": round(frontend_runq, 3),
        "frontend_qos_ratio": round(frontend_qos_ratio, 3),
        "target_service": route.target_deployment,
        "target_p90_ms": round(target_p90, 3),
        "target_runq_ms": round(target_runq, 3),
        "target_exclusive_ms": round(target_exclusive, 3),
        "target_local_share": round(target_local_share, 3),
        "target_rps": round(target_rps, 3),
    }


def write_snapshot_csv(path: Path, rows: List[dict]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def max_value(rows: List[dict], key: str) -> float:
    if not rows:
        return 0.0
    return max(safe_float(row.get(key, 0.0)) for row in rows)


def compute_route_summary(route: RouteCandidate, rows: List[dict], frontend_slo_ms: float) -> dict:
    peak_route_p90 = max_value(rows, "route_p90_ms")
    peak_route_rps = max_value(rows, "route_rps")
    peak_route_error_rate = max_value(rows, "route_error_rate")
    peak_target_p90 = max_value(rows, "target_p90_ms")
    peak_target_runq = max_value(rows, "target_runq_ms")
    peak_target_exclusive = max_value(rows, "target_exclusive_ms")
    peak_target_rps = max_value(rows, "target_rps")
    peak_frontend_truth_p90 = max_value(rows, "frontend_truth_p90_ms")
    peak_frontend_runq = max_value(rows, "frontend_runq_ms")
    peak_target_local_share = max_value(rows, "target_local_share")

    qos_pressure = clamp01((peak_route_p90 - frontend_slo_ms) / max(frontend_slo_ms, 1.0))
    runq_pressure = clamp01(peak_target_runq / 12.0)
    local_share = clamp01(peak_target_local_share)
    demand = clamp01(peak_target_rps / 150.0)
    error_penalty = clamp01(peak_route_error_rate / 0.10)
    cpu_bound_score = clamp01((0.40 * runq_pressure) + (0.30 * local_share) + (0.20 * qos_pressure) + (0.10 * demand) - (0.15 * error_penalty))

    if peak_route_error_rate >= 0.20:
        classification = "invalid_or_unstable"
    elif peak_target_runq >= DEFAULT_RUNQ_STRONG_MS and local_share >= 0.50 and peak_route_p90 >= frontend_slo_ms:
        classification = "strong_cpu_bound"
    elif peak_target_runq >= DEFAULT_RUNQ_SOFT_MS and local_share >= 0.35:
        classification = "mixed_cpu_bound"
    else:
        classification = "weak_cpu_bound"

    return {
        "route": route.name,
        "label": route.label,
        "path": route.path,
        "target_deployment": route.target_deployment,
        "peak_route_p90_ms": round(peak_route_p90, 3),
        "peak_route_rps": round(peak_route_rps, 3),
        "peak_route_error_rate": round(peak_route_error_rate, 6),
        "peak_frontend_truth_p90_ms": round(peak_frontend_truth_p90, 3),
        "peak_frontend_runq_ms": round(peak_frontend_runq, 3),
        "peak_target_p90_ms": round(peak_target_p90, 3),
        "peak_target_runq_ms": round(peak_target_runq, 3),
        "peak_target_exclusive_ms": round(peak_target_exclusive, 3),
        "peak_target_local_share": round(peak_target_local_share, 3),
        "peak_target_rps": round(peak_target_rps, 3),
        "frontend_slo_ms": round(frontend_slo_ms, 3),
        "cpu_bound_score": round(cpu_bound_score, 3),
        "classification": classification,
    }


def write_summary_csv(path: Path, rows: List[dict]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_summary_markdown(path: Path, rows: List[dict]) -> None:
    lines = [
        "# Sock Shop Route CPU Profiling",
        "",
        "| Route | Path | Target | Score | Class | Peak Route P90 (ms) | Peak Target RunQ (ms) | Local Share | Peak Target RPS |",
        "| --- | --- | --- | ---: | --- | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row['route']} | `{row['path']}` | {row['target_deployment']} | "
            f"{row['cpu_bound_score']:.3f} | {row['classification']} | "
            f"{row['peak_route_p90_ms']:.2f} | {row['peak_target_runq_ms']:.2f} | "
            f"{row['peak_target_local_share']:.2f} | {row['peak_target_rps']:.2f} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_route_harness(
    route: RouteCandidate,
    mix_path: Path,
    csv_path: Path,
    log_path: Path,
    graph_csv_path: Path,
    frontend_url: str,
    aggregator_url: str,
    app_ns: str,
    sample_interval: float,
    timeout_s: float,
    frontend_slo_ms: float,
) -> List[dict]:
    cmd = [
        sys.executable,
        str(ROOT / "src/load-generator/eval_harness.py"),
        "--url",
        frontend_url,
        "--deployment",
        "front-end",
        "--namespace",
        app_ns,
        "--profile",
        "sockshop",
        "--mix-file",
        str(mix_path),
        "--duration",
        str(sum(int(p["end_s"]) - int(p["start_s"]) for p in (yaml.safe_load(mix_path.read_text(encoding="utf-8")) or {}).get("phases", []))),
        "--sample-interval",
        str(sample_interval),
        "--warmup-seconds",
        "0",
        "--timeout",
        str(timeout_s),
        "--csv",
        str(csv_path),
        "--aggregator-url",
        aggregator_url,
        "--control-target",
        "front-end",
    ]

    rows: List[dict] = []
    start_ts = time.time()
    with log_path.open("w", encoding="utf-8") as log_handle:
        proc = subprocess.Popen(cmd, stdout=log_handle, stderr=subprocess.STDOUT, text=True)
        try:
            while True:
                rc = proc.poll()
                elapsed_s = time.time() - start_ts
                try:
                    graph = fetch_graph(aggregator_url)
                    rows.append(extract_snapshot(elapsed_s, graph, route, frontend_slo_ms))
                except Exception:
                    pass
                if rc is not None:
                    break
                time.sleep(sample_interval)
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=15)

    if proc.returncode != 0:
        raise RuntimeError(f"Harness failed for route {route.name}; see {log_path}")

    try:
        graph = fetch_graph(aggregator_url)
        rows.append(extract_snapshot(time.time() - start_ts, graph, route, frontend_slo_ms))
    except Exception:
        pass

    write_snapshot_csv(graph_csv_path, rows)
    return rows


def load_frontend_slo_ms(control_state: dict) -> float:
    slos = control_state.get("slos", {})
    front = slos.get("front-end", {}) if isinstance(slos, dict) else {}
    return safe_float(front.get("sloLatency", DEFAULT_FRONTEND_SLO_MS), DEFAULT_FRONTEND_SLO_MS)


def parse_args():
    p = argparse.ArgumentParser(description="Profile Sock Shop routes to find CPU-bound benchmark paths")
    p.add_argument("--app-namespace", default="sock-shop")
    p.add_argument("--control-namespace", default="thrive-scale")
    p.add_argument("--frontend-url", default="", help="Sock Shop front-end base URL, e.g. http://front-end.sock-shop")
    p.add_argument("--aggregator-url", default="", help="Aggregator base URL, auto-discovered if omitted")
    p.add_argument("--route-catalog", default=str(DEFAULT_ROUTE_CATALOG), help="YAML file whose routes are used as route candidates")
    p.add_argument("--route", action="append", default=[], help="Only profile the named route(s)")
    p.add_argument("--rps-levels", default="30,60,120,180,240", help="Comma-separated stepped RPS levels")
    p.add_argument("--phase-seconds", type=int, default=30)
    p.add_argument("--sample-interval", type=float, default=2.0)
    p.add_argument("--timeout", type=float, default=8.0)
    p.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    p.add_argument("--reset-aggregator", action="store_true", help="Call /api/reset before each route run")
    p.add_argument("--control-token", default="", help="Optional X-Control-Token value for protected control endpoints")
    p.add_argument("--freeze-thrivescale", action="store_true", help="Scale custom-autoscaler to 0 during profiling")
    p.add_argument("--delete-hpa", action="store_true", help="Delete HPAs in the app namespace before profiling")
    p.add_argument("--baseline-replicas", type=int, default=1, help="Replica count to pin front-end and target services to")
    return p.parse_args()


def main():
    args = parse_args()
    aggregator_url = args.aggregator_url.strip() or discover_aggregator_url(args.control_namespace)
    if not aggregator_url:
        raise SystemExit("Could not discover aggregator URL; provide --aggregator-url")

    control_state = fetch_json(f"{aggregator_url.rstrip('/')}/api/control/state", timeout=5)
    benchmark = control_state.get("benchmark", {})
    if str(benchmark.get("id", "")).strip() != "sock-shop":
        raise SystemExit("Live benchmark profile is not Sock Shop; route profiling expects the Sock Shop deployment")

    frontend_url = args.frontend_url.strip() or str(control_state.get("trafficBaseUrl", "")).strip()
    if not frontend_url:
        raise SystemExit("Could not determine Sock Shop front-end URL; provide --frontend-url")

    route_catalog = load_route_catalog(Path(args.route_catalog))
    routes = merge_sockshop_routes(control_state, route_catalog)
    if args.route:
        wanted = {name.strip() for name in args.route if name.strip()}
        routes = [route for route in routes if route.name in wanted]
    if not routes:
        raise SystemExit("No Sock Shop routes resolved for profiling")

    hpas = hpa_names(args.app_namespace)
    if hpas and not args.delete_hpa:
        raise SystemExit(
            "HPAs are present in the app namespace. Re-run with --delete-hpa or clear them first: "
            + ",".join(hpas)
        )

    current_autoscaler_replicas = autoscaler_replicas(args.control_namespace)
    if current_autoscaler_replicas > 0 and not args.freeze_thrivescale:
        raise SystemExit(
            "custom-autoscaler is still running. Re-run with --freeze-thrivescale or scale it to 0 first."
        )

    if args.freeze_thrivescale:
        kube_scale(args.control_namespace, "custom-autoscaler", 0)

    if args.delete_hpa and hpas:
        run(["kubectl", "delete", "hpa", "--all", "-n", args.app_namespace], check=False)

    frontend_slo_ms = load_frontend_slo_ms(control_state)
    output_root = Path(args.output_dir) / now_utc_slug()
    output_root.mkdir(parents=True, exist_ok=True)

    rps_levels = [safe_float(part, 0.0) for part in args.rps_levels.split(",") if str(part).strip()]
    rps_levels = [level for level in rps_levels if level > 0]
    if not rps_levels:
        raise SystemExit("No valid --rps-levels provided")

    baseline_services = sorted({"front-end"} | {route.target_deployment for route in routes if route.target_deployment})
    for svc in baseline_services:
        kube_scale(args.app_namespace, svc, args.baseline_replicas)
    for svc in baseline_services:
        kube_rollout(args.app_namespace, svc)

    summaries = []
    for route in routes:
        route_slug = route.name.replace("/", "_")
        mix_path = output_root / f"mix_{route_slug}.yaml"
        csv_path = output_root / f"harness_{route_slug}.csv"
        log_path = output_root / f"harness_{route_slug}.log"
        graph_csv_path = output_root / f"graph_{route_slug}.csv"

        mix_cfg = build_single_route_mix(route, rps_levels, args.phase_seconds)
        mix_path.write_text(yaml.safe_dump(mix_cfg, sort_keys=False), encoding="utf-8")

        for svc in {"front-end", route.target_deployment}:
            if svc:
                kube_scale(args.app_namespace, svc, args.baseline_replicas)
                kube_rollout(args.app_namespace, svc)

        if args.reset_aggregator:
            reset_aggregator(aggregator_url, token=args.control_token.strip())
            time.sleep(max(1.0, args.sample_interval))

        print(
            f"[profile] route={route.name} path={route.path} target={route.target_deployment} "
            f"rps_levels={','.join(str(int(level)) if float(level).is_integer() else str(level) for level in rps_levels)}",
            flush=True,
        )
        rows = run_route_harness(
            route=route,
            mix_path=mix_path,
            csv_path=csv_path,
            log_path=log_path,
            graph_csv_path=graph_csv_path,
            frontend_url=frontend_url,
            aggregator_url=aggregator_url,
            app_ns=args.app_namespace,
            sample_interval=args.sample_interval,
            timeout_s=args.timeout,
            frontend_slo_ms=frontend_slo_ms,
        )
        summary = compute_route_summary(route, rows, frontend_slo_ms)
        summaries.append(summary)
        print(
            f"[result] route={route.name} class={summary['classification']} score={summary['cpu_bound_score']:.3f} "
            f"peak_route_p90={summary['peak_route_p90_ms']:.2f}ms peak_runq={summary['peak_target_runq_ms']:.2f}ms "
            f"local_share={summary['peak_target_local_share']:.2f}",
            flush=True,
        )

    summaries.sort(key=lambda row: (-safe_float(row.get("cpu_bound_score", 0.0)), row.get("route", "")))
    write_summary_csv(output_root / "route_summary.csv", summaries)
    write_summary_markdown(output_root / "route_summary.md", summaries)
    (output_root / "route_summary.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")

    print("\nRecommended CPU-bound routes:", flush=True)
    for row in summaries[:5]:
        print(
            f"  {row['route']:<18} score={row['cpu_bound_score']:.3f} class={row['classification']:<18} "
            f"path={row['path']} target={row['target_deployment']}",
            flush=True,
        )
    print(f"\n[done] route profiling artifacts written to {output_root}", flush=True)


if __name__ == "__main__":
    main()
