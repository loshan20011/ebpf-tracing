#!/usr/bin/env python3
import argparse
import csv
import json
import os
import tempfile
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(cmd: List[str], check: bool = True) -> subprocess.CompletedProcess:
    cp = subprocess.run(cmd, text=True, capture_output=True)
    if check and cp.returncode != 0:
        raise RuntimeError(
            f"command failed ({cp.returncode}): {' '.join(cmd)}\nstdout:\n{cp.stdout}\nstderr:\n{cp.stderr}"
        )
    return cp


def parse_service_map(raw: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in [p.strip() for p in raw.split(",") if p.strip()]:
        if "=" not in part:
            raise ValueError(f"invalid --mix-map entry: {part}")
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def parse_replica_list(raw: str) -> List[int]:
    out: List[int] = []
    for part in [p.strip() for p in raw.split(",") if p.strip()]:
        n = int(part)
        if n <= 0:
            continue
        out.append(n)
    if not out:
        raise ValueError("replica list is empty")
    return sorted(set(out))


def parse_rps_sweep(raw: str) -> List[float]:
    out: List[float] = []
    for part in [p.strip() for p in raw.split(",") if p.strip()]:
        x = float(part)
        if x > 0:
            out.append(x)
    if not out:
        raise ValueError("RPS sweep is empty")
    return sorted(out)


def read_csv_summary(path: Path) -> Tuple[float, float, float, float]:
    p90_vals: List[float] = []
    avg_vals: List[float] = []
    ok_total = 0
    sent_total = 0
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                if int(float(row.get("in_score_window", "0"))) != 1:
                    continue
                p90_vals.append(float(row.get("latency_p90_ms", "0") or 0))
                avg_vals.append(float(row.get("latency_avg_ms", "0") or 0))
                ok_total += int(float(row.get("requests_ok", "0") or 0))
                sent_total += int(float(row.get("requests_sent", "0") or 0))
            except Exception:
                continue
    if not p90_vals:
        return 0.0, 0.0, 0.0, 0.0
    max_p90 = max(p90_vals)
    mean_p90 = sum(p90_vals) / len(p90_vals)
    mean_avg = sum(avg_vals) / max(1, len(avg_vals))
    ok_rate = (ok_total / sent_total) if sent_total > 0 else 0.0
    return max_p90, mean_p90, mean_avg, ok_rate


def concurrency_from_rps_and_latency_ms(rps: float, latency_ms: float) -> float:
    return max(0.0, float(rps)) * max(0.0, float(latency_ms)) / 1000.0


def load_mix_routes(path: Path) -> List[dict]:
    routes: List[dict] = []
    in_routes = False
    current: Optional[dict] = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "routes:":
            in_routes = True
            current = None
            continue
        if not in_routes:
            continue
        if not line.startswith("  "):
            break
        if stripped.startswith("- "):
            if current:
                routes.append(current)
            current = {}
            remainder = stripped[2:]
            if ":" in remainder:
                k, v = remainder.split(":", 1)
                current[k.strip()] = v.strip()
            continue
        if current is None or ":" not in stripped:
            continue
        k, v = stripped.split(":", 1)
        current[k.strip()] = v.strip()
    if current:
        routes.append(current)
    return routes


def write_steady_mix(source_mix: Path, target_rps: float, out_path: Path) -> None:
    routes = load_mix_routes(source_mix)
    if not routes:
        raise RuntimeError(f"no routes found in mix file: {source_mix}")

    lines = [
        "phases:",
        "  - start_s: 0",
        "    end_s: 9999",
        f"    target_rps: {float(target_rps)}",
        "routes:",
    ]
    for route in routes:
        lines.append("  - name: {}".format(route.get("name", "route")))
        lines.append("    group: {}".format(route.get("group", "default")))
        lines.append("    method: {}".format(route.get("method", "GET")))
        lines.append("    path: {}".format(route.get("path", "/")))
        lines.append("    weight: {}".format(route.get("weight", "1")))
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def get_slo_ms(kubectl: str, ns: str, service: str) -> float:
    cp = run(
        [
            kubectl,
            "get",
            "serviceslo",
            "-n",
            ns,
            "-o",
            "json",
        ]
    )
    payload = json.loads(cp.stdout or "{}")
    for item in payload.get("items", []):
        spec = item.get("spec", {})
        if str(spec.get("targetDeployment", "")) == service:
            try:
                return float(spec.get("sloLatency", 0.0))
            except Exception:
                return 0.0
    return 0.0


def scale_and_wait(kubectl: str, ns: str, service: str, replicas: int, timeout_s: int) -> None:
    run([kubectl, "scale", f"deploy/{service}", "-n", ns, f"--replicas={replicas}"])
    run([kubectl, "rollout", "status", f"deploy/{service}", "-n", ns, f"--timeout={timeout_s}s"])


def build_harness_cmd(
    python_bin: str,
    harness: str,
    url: str,
    app_ns: str,
    service: str,
    mix_file: str,
    aggregator_url: str,
    rps: float,
    duration_s: int,
    warmup_s: int,
    sample_interval_s: int,
    out_csv: Path,
) -> List[str]:
    return [
        python_bin,
        harness,
        "--url",
        url,
        "--deployment",
        service,
        "--namespace",
        app_ns,
        "--profile",
        "sockshop",
        "--mix-file",
        mix_file,
        "--mode",
        "steady",
        "--duration",
        str(duration_s),
        "--warmup-seconds",
        str(warmup_s),
        "--base-rps",
        str(rps),
        "--burst-rps",
        str(rps),
        "--burst-at",
        "9999",
        "--sample-interval",
        str(sample_interval_s),
        "--aggregator-url",
        aggregator_url,
        "--control-target",
        service,
        "--csv",
        str(out_csv),
    ]


def main() -> int:
    p = argparse.ArgumentParser(description="Build deterministic capacity profiles for services")
    p.add_argument("--app-ns", default=os.getenv("APP_NS", "sock-shop"))
    p.add_argument("--kubectl", default=os.getenv("KUBECTL", "kubectl"))
    p.add_argument("--frontend-url", default=os.getenv("FRONTEND_URL", ""), required=True)
    p.add_argument("--aggregator-url", default=os.getenv("AGGREGATOR_URL", ""), required=True)
    p.add_argument("--services", default="front-end,catalogue,carts,orders")
    p.add_argument(
        "--mix-map",
        default=(
            "front-end=deploy/03-evaluation/workloads/sockshop-catalogue-proof.yaml,"
            "catalogue=deploy/03-evaluation/workloads/sockshop-catalogue-proof.yaml,"
            "carts=deploy/03-evaluation/workloads/sockshop-ew-mix.yaml,"
            "orders=deploy/03-evaluation/workloads/sockshop-ew-mix.yaml"
        ),
    )
    p.add_argument("--replicas", default="1,2,3,4,6,8,10")
    p.add_argument("--rps-sweep", default="5,10,20,40,60,80,100,140,180,220,260,300")
    p.add_argument("--duration", type=int, default=90)
    p.add_argument("--warmup", type=int, default=20)
    p.add_argument("--sample-interval", type=int, default=2)
    p.add_argument("--ok-rate-min", type=float, default=0.98)
    p.add_argument("--safe-headroom-factor", type=float, default=1.15)
    p.add_argument("--rollout-timeout", type=int, default=240)
    p.add_argument("--sleep-between", type=float, default=5.0)
    p.add_argument("--pause-autoscaler", action="store_true", default=True)
    p.add_argument("--control-ns", default=os.getenv("CONTROL_NS", "thrive-scale"))
    p.add_argument("--autoscaler-deploy", default=os.getenv("AUTOSCALER_DEPLOY", "custom-autoscaler"))
    p.add_argument("--python-bin", default=sys.executable)
    p.add_argument("--harness", default="src/load-generator/eval_harness.py")
    p.add_argument("--out", default="results/capacity_profiles/capacity_profiles.json")
    p.add_argument("--raw-csv-dir", default="results/capacity_profiles/raw")
    args = p.parse_args()

    services = [s.strip() for s in args.services.split(",") if s.strip()]
    mix_map = parse_service_map(args.mix_map)
    replicas_list = parse_replica_list(args.replicas)
    rps_sweep = parse_rps_sweep(args.rps_sweep)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    raw_dir = Path(args.raw_csv_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    tmp_mix_dir = raw_dir / "_generated_mixes"
    tmp_mix_dir.mkdir(parents=True, exist_ok=True)

    profiles: Dict[str, List[dict]] = {}
    measurements: List[dict] = []

    autoscaler_scaled_down = False
    try:
        if args.pause_autoscaler:
            run(
                [
                    args.kubectl,
                    "scale",
                    f"deploy/{args.autoscaler_deploy}",
                    "-n",
                    args.control_ns,
                    "--replicas=0",
                ],
                check=False,
            )
            autoscaler_scaled_down = True

        for svc in services:
            if svc not in mix_map:
                raise RuntimeError(f"missing mix for service={svc}; add to --mix-map")
            mix_file = mix_map[svc]
            if not Path(mix_file).exists():
                raise RuntimeError(f"mix file does not exist for {svc}: {mix_file}")

            slo_ms = get_slo_ms(args.kubectl, args.app_ns, svc)
            if slo_ms <= 0:
                raise RuntimeError(f"SLO not found for service={svc}")

            profiles[svc] = []
            print(f"[service] {svc} slo_ms={slo_ms}")

            for rep in replicas_list:
                scale_and_wait(args.kubectl, args.app_ns, svc, rep, args.rollout_timeout)
                safe_rps = 0.0
                safe_p90 = 0.0
                safe_mean_p90 = 0.0
                safe_mean_avg = 0.0
                safe_ok_rate = 0.0

                for rps in rps_sweep:
                    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    out_csv = raw_dir / f"{svc}_rep{rep}_rps{int(rps)}_{run_ts}.csv"
                    steady_mix = tmp_mix_dir / f"{svc}_rep{rep}_rps{int(rps)}_{run_ts}.yaml"
                    write_steady_mix(Path(mix_file), rps, steady_mix)
                    cmd = build_harness_cmd(
                        python_bin=args.python_bin,
                        harness=args.harness,
                        url=args.frontend_url,
                        app_ns=args.app_ns,
                        service=svc,
                        mix_file=str(steady_mix),
                        aggregator_url=args.aggregator_url,
                        rps=rps,
                        duration_s=args.duration,
                        warmup_s=args.warmup,
                        sample_interval_s=args.sample_interval,
                        out_csv=out_csv,
                    )
                    print(f"  [probe] service={svc} replicas={rep} rps={rps}")
                    run(cmd)
                    max_p90, mean_p90, mean_avg, ok_rate = read_csv_summary(out_csv)

                    passed = (max_p90 > 0.0) and (max_p90 <= slo_ms) and (ok_rate >= args.ok_rate_min)
                    measurements.append(
                        {
                            "service": svc,
                            "replicas": rep,
                            "rps": rps,
                            "slo_ms": slo_ms,
                            "max_p90_ms": round(max_p90, 3),
                            "mean_p90_ms": round(mean_p90, 3),
                            "mean_avg_ms": round(mean_avg, 3),
                            "ok_rate": round(ok_rate, 5),
                            "pass": bool(passed),
                            "csv": str(out_csv),
                            "mix_file": str(steady_mix),
                            "ts": utc_now(),
                        }
                    )

                    if passed:
                        safe_rps = rps
                        safe_p90 = max_p90
                        safe_mean_p90 = mean_p90
                        safe_mean_avg = mean_avg
                        safe_ok_rate = ok_rate
                    else:
                        break

                    time.sleep(max(0.0, args.sleep_between))

                if safe_rps > 0.0:
                    safe_conc_p90 = concurrency_from_rps_and_latency_ms(safe_rps, safe_p90)
                    safe_conc_avg = concurrency_from_rps_and_latency_ms(safe_rps, safe_mean_avg)
                    profiles[svc].append(
                        {
                            "replicas": rep,
                            "max_safe_rps": round(safe_rps, 3),
                            "observed_p90_ms": round(safe_p90, 3),
                            "observed_mean_p90_ms": round(safe_mean_p90, 3),
                            "observed_mean_avg_ms": round(safe_mean_avg, 3),
                            "safe_concurrency_p90": round(safe_conc_p90, 3),
                            "safe_concurrency_avg": round(safe_conc_avg, 3),
                            "rps_per_replica": round(safe_rps / max(1, rep), 3),
                            "headroom_factor": round(args.safe_headroom_factor, 3),
                            "safe_target_rps_with_headroom": round(safe_rps / max(1.0, args.safe_headroom_factor), 3),
                            "ok_rate": round(safe_ok_rate, 5),
                            "generated_at": utc_now(),
                        }
                    )
    finally:
        if autoscaler_scaled_down:
            run(
                [
                    args.kubectl,
                    "scale",
                    f"deploy/{args.autoscaler_deploy}",
                    "-n",
                    args.control_ns,
                    "--replicas=1",
                ],
                check=False,
            )

    payload = {
        "generated_at": utc_now(),
        "app_namespace": args.app_ns,
        "frontend_url": args.frontend_url,
        "aggregator_url": args.aggregator_url,
        "resource_shape": os.getenv("RESOURCE_SHAPE", "unknown"),
        "profile_config": {
            "services": services,
            "replicas": replicas_list,
            "rps_sweep": rps_sweep,
            "duration_s": args.duration,
            "warmup_s": args.warmup,
            "sample_interval_s": args.sample_interval,
            "ok_rate_min": args.ok_rate_min,
            "safe_headroom_factor": args.safe_headroom_factor,
        },
        "services": profiles,
        "measurements": measurements,
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[done] wrote profile: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
