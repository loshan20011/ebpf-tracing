#!/usr/bin/env python3
import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_PROBE = SCRIPT_DIR.parent.parent / "sockshop" / "login_load_probe.py"

FREEZE_RESOURCES = {
    "front-end": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}},
    "user": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "300m", "memory": "256Mi"}},
    "carts": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"cpu": "300m", "memory": "512Mi"}},
}

FREEZE_BOUNDS = {
    "front-end": {"min": 1, "max": 4},
    "user": {"min": 1, "max": 6},
    "carts": {"min": 1, "max": 4},
}


def kubectl_cmd(args: list[str]) -> list[str]:
    kubectl = shutil.which("kubectl")
    if kubectl:
        return [kubectl, *args]
    k3s = shutil.which("k3s")
    if k3s:
        return [k3s, "kubectl", *args]
    raise RuntimeError("kubectl or k3s not found")


def kubectl_ok(args: list[str], timeout: int = 60) -> bool:
    try:
        subprocess.check_call(
            kubectl_cmd(args),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
        return True
    except Exception:
        return False


def kubectl_json(args: list[str], timeout: int = 60) -> dict | list | None:
    try:
        out = subprocess.check_output(kubectl_cmd(args), text=True, timeout=timeout)
        return json.loads(out)
    except Exception:
        return None


def set_resources(namespace: str) -> None:
    for deploy, spec in FREEZE_RESOURCES.items():
        subprocess.check_call(
            kubectl_cmd(
                [
                    "set",
                    "resources",
                    f"deployment/{deploy}",
                    "-n",
                    namespace,
                    f"--requests=cpu={spec['requests']['cpu']},memory={spec['requests']['memory']}",
                    f"--limits=cpu={spec['limits']['cpu']},memory={spec['limits']['memory']}",
                ]
            )
        )


def scale_fixed(namespace: str, replicas: dict[str, int]) -> None:
    for deploy, count in replicas.items():
        subprocess.check_call(kubectl_cmd(["scale", "deployment", deploy, "-n", namespace, f"--replicas={count}"]))


def disable_autoscaling(system_namespace: str, workload_namespace: str) -> None:
    kubectl_ok(["scale", "deployment", "custom-autoscaler", "-n", system_namespace, "--replicas=0"], timeout=180)
    for hpa in ["front-end", "user", "carts"]:
        kubectl_ok(["delete", "hpa", hpa, "-n", workload_namespace], timeout=60)


def verify_stable_env(namespace: str, timeout_seconds: int) -> None:
    required = ["front-end", "user", "carts", "user-db", "carts-db"]
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        deploys = kubectl_json(["get", "deploy", "-n", namespace, "-o", "json"], timeout=30)
        pods = kubectl_json(["get", "pods", "-n", namespace, "-o", "json"], timeout=30)
        if not isinstance(deploys, dict) or not isinstance(pods, dict):
            time.sleep(3)
            continue
        deploy_map = {item["metadata"]["name"]: item for item in deploys.get("items", [])}
        ready = True
        for name in required:
            item = deploy_map.get(name)
            if not item:
                ready = False
                break
            desired = int(item.get("spec", {}).get("replicas", 0) or 0)
            available = int(item.get("status", {}).get("availableReplicas", 0) or 0)
            if available < desired:
                ready = False
                break
        if ready:
            bad = []
            for pod in pods.get("items", []):
                pod_name = pod.get("metadata", {}).get("name", "")
                if not any(pod_name.startswith(prefix) for prefix in required):
                    continue
                for cs in pod.get("status", {}).get("containerStatuses", []) or []:
                    waiting = (cs.get("state", {}) or {}).get("waiting", {}) or {}
                    if waiting.get("reason") in {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull"}:
                        bad.append((pod_name, waiting.get("reason")))
            if not bad:
                return
        time.sleep(5)
    raise SystemExit(f"Environment did not stabilize in {timeout_seconds}s")


def window_stats(rows: list[dict], slo_ms: float, window_seconds: int = 10) -> list[dict]:
    windows: dict[int, list[dict]] = {}
    for row in rows:
        bucket = int(float(row.get("t_rel_s", 0.0)) // window_seconds)
        windows.setdefault(bucket, []).append(row)
    out = []
    for bucket in sorted(windows):
        items = windows[bucket]
        ok = [r for r in items if r.get("success")]
        lats = sorted(float(r.get("latency_ms", 0.0) or 0.0) for r in ok)
        if not lats:
            p90 = 0.0
        elif len(lats) == 1:
            p90 = lats[0]
        else:
            idx = max(0, min(len(lats) - 1, int(0.9 * (len(lats) - 1))))
            p90 = lats[idx]
        success_rate = len(ok) / max(len(items), 1)
        failed = p90 > slo_ms or success_rate < 0.99
        out.append(
            {
                "window_index": bucket,
                "sent": len(items),
                "successful": len(ok),
                "success_rate": round(success_rate, 4),
                "p90_latency_ms": round(p90, 3),
                "failed": failed,
            }
        )
    return out


def run_probe(
    probe_script: Path,
    base_url: str,
    creds_file: Path,
    rps: int,
    duration_seconds: int,
    output_dir: Path,
    slo_ms: float,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(probe_script),
        "--base-url",
        base_url,
        "--creds-file",
        str(creds_file),
        "--rps",
        str(rps),
        "--duration-seconds",
        str(duration_seconds),
        "--split-seconds",
        str(duration_seconds // 2),
        "--slo-ms",
        str(slo_ms),
        "--session-pool",
        "32",
        "--max-in-flight",
        "128",
        "--output-dir",
        str(output_dir),
    ]
    subprocess.check_call(cmd)
    request_rows = [
        json.loads(line)
        for line in (output_dir / "login_probe.ndjson").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    summary = json.loads((output_dir / "login_probe_summary.json").read_text(encoding="utf-8"))
    summary["windows_10s"] = window_stats(request_rows, slo_ms, window_seconds=10)
    fail_streak = 0
    failed_level = False
    for window in summary["windows_10s"]:
        if window["failed"]:
            fail_streak += 1
            if fail_streak >= 3:
                failed_level = True
                break
        else:
            fail_streak = 0
    summary["level_failed"] = failed_level
    summary["fail_streak_reached"] = fail_streak >= 3
    (output_dir / "login_probe_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Calibrate final Sock Shop /login benchmark RPS.")
    parser.add_argument("--namespace", default="sock-shop")
    parser.add_argument("--system-namespace", default="thrive-scale")
    parser.add_argument("--base-url", default="http://127.0.0.1:30001")
    parser.add_argument("--creds-file", required=True, type=Path)
    parser.add_argument("--probe-script", default=str(DEFAULT_PROBE), type=Path)
    parser.add_argument("--output-root", default=Path("results/benchmark/login/calibration"), type=Path)
    parser.add_argument("--slo-ms", type=float, default=200.0)
    parser.add_argument("--duration-seconds", type=int, default=60)
    parser.add_argument("--stable-timeout-seconds", type=int, default=600)
    args = parser.parse_args()

    args.output_root.mkdir(parents=True, exist_ok=True)
    set_resources(args.namespace)
    disable_autoscaling(args.system_namespace, args.namespace)

    # Step A
    scale_fixed(args.namespace, {"front-end": 1, "user": 1, "carts": 1})
    verify_stable_env(args.namespace, args.stable_timeout_seconds)
    step_a_levels = [20, 40, 60, 80, 100, 120]
    a_results = []
    r_break_1 = None
    for rps in step_a_levels:
        result = run_probe(args.probe_script, args.base_url, args.creds_file, rps, args.duration_seconds, args.output_root / "A" / f"rps_{rps}", args.slo_ms)
        a_results.append({"rps": rps, "summary": result})
        if result["level_failed"]:
            r_break_1 = rps
            break
    if r_break_1 is None:
        raise SystemExit("Calibration A did not find a breaking point in the configured range")

    # Step B
    scale_fixed(
        args.namespace,
        {
            "front-end": FREEZE_BOUNDS["front-end"]["max"],
            "user": FREEZE_BOUNDS["user"]["max"],
            "carts": FREEZE_BOUNDS["carts"]["max"],
        },
    )
    verify_stable_env(args.namespace, args.stable_timeout_seconds)
    candidates = []
    for factor in [1.0, 1.25, 1.5, 1.75]:
        value = max(1, int(round(r_break_1 * factor)))
        if value not in candidates:
            candidates.append(value)
    b_results = []
    last_passing = max(1, r_break_1 - 1)
    for rps in candidates:
        result = run_probe(args.probe_script, args.base_url, args.creds_file, rps, args.duration_seconds, args.output_root / "B" / f"rps_{rps}", args.slo_ms)
        b_results.append({"rps": rps, "summary": result})
        if result["level_failed"]:
            break
        last_passing = rps
    r_ceiling = last_passing

    r_peak = min(int(round(1.25 * r_break_1)), int(round(0.85 * r_ceiling)))
    r_peak = max(1, r_peak)
    phases = [
        {"name": "phase_1", "duration_seconds": 120, "rps": max(1, int(round(0.6 * r_peak)))},
        {"name": "phase_2", "duration_seconds": 180, "rps": max(1, int(round(0.8 * r_peak)))},
        {"name": "phase_3", "duration_seconds": 240, "rps": max(1, int(round(1.0 * r_peak)))},
        {"name": "phase_4", "duration_seconds": 180, "rps": max(1, int(round(0.7 * r_peak)))},
        {"name": "phase_5", "duration_seconds": 180, "rps": max(1, int(round(1.1 * r_peak)))},
    ]

    summary = {
        "benchmark_path": "GET /login",
        "benchmark_slo_ms": args.slo_ms,
        "step_a_single_replica": {"front-end": 1, "user": 1, "carts": 1},
        "step_a_results": a_results,
        "R_break_1": r_break_1,
        "step_b_max_replica": {
            "front-end": FREEZE_BOUNDS["front-end"]["max"],
            "user": FREEZE_BOUNDS["user"]["max"],
            "carts": FREEZE_BOUNDS["carts"]["max"],
        },
        "step_b_results": b_results,
        "R_ceiling": r_ceiling,
        "R_peak": r_peak,
        "final_phases": phases,
        "resources": FREEZE_RESOURCES,
        "replica_bounds": FREEZE_BOUNDS,
    }
    (args.output_root / "calibration_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    (args.output_root / "final_phases.json").write_text(json.dumps(phases, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
