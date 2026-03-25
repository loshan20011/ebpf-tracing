#!/usr/bin/env python3
import argparse
import base64
import json
import math
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROBE_SCRIPT = SCRIPT_DIR.parent.parent / "sockshop" / "login_load_probe.py"
REQUIRED_DEPLOYS = ["front-end", "user", "carts", "user-db", "carts-db"]
TRACKED_DEPLOYS = ["front-end", "user", "carts"]
CPU_REQUESTS = {"front-end": 0.1, "user": 0.1, "carts": 0.3}
CARTS_LOG_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\boutofmemoryerror\b",
        r"\boomkilled\b",
        r"\bcrashloopbackoff\b",
        r"\bconnection refused\b",
        r"\bno route to host\b",
        r"\bfailed to (?:connect|send|select server)\b",
        r"\b(?:mongo|socket).*timed out\b",
        r"\b(?:mongo|socket).*exception\b",
        r"\bcaused by:\s*(?:java\.net|com\.mongodb|org\.springframework)\b",
    )
]
CARTS_LOG_IGNORE_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r'RequestMappingHandlerMapping\s*:\s*Mapped\s+"\{\[/error',
        r'RepositoryRestHandlerMapping\s*:\s*Mapped\s+"\{\[',
        r'BasicErrorController\.error',
        r'BasicErrorController\.errorHtml',
    )
]


def kubectl_cmd(args: list[str]) -> list[str]:
    kubectl = shutil.which("kubectl")
    if kubectl:
        return [kubectl, *args]
    k3s = shutil.which("k3s")
    if k3s:
        return [k3s, "kubectl", *args]
    raise RuntimeError("kubectl or k3s not found")


def kubectl_json(args: list[str], timeout: int = 60) -> dict | list | None:
    try:
        out = subprocess.check_output(kubectl_cmd(args), text=True, timeout=timeout)
        return json.loads(out)
    except Exception:
        return None


def kubectl_text(args: list[str], timeout: int = 60) -> str:
    return subprocess.check_output(kubectl_cmd(args), text=True, timeout=timeout)


def collect_required_pod_restarts(namespace: str) -> dict[str, int]:
    pods = kubectl_json(["get", "pods", "-n", namespace, "-o", "json"], timeout=30)
    result: dict[str, int] = {}
    if not isinstance(pods, dict):
        return result
    for pod in pods.get("items", []):
        pod_name = pod.get("metadata", {}).get("name", "")
        if not any(pod_name.startswith(prefix) for prefix in REQUIRED_DEPLOYS):
            continue
        total = 0
        for cs in pod.get("status", {}).get("containerStatuses", []) or []:
            total += int(cs.get("restartCount", 0) or 0)
        result[pod_name] = total
    return result


def collect_service_pods(namespace: str, deployment_prefix: str) -> list[dict]:
    pods = kubectl_json(["get", "pods", "-n", namespace, "-o", "json"], timeout=30)
    if not isinstance(pods, dict):
        return []
    excluded_prefixes = []
    if deployment_prefix == "carts-":
        excluded_prefixes.append("carts-db-")
    return [
        pod
        for pod in pods.get("items", [])
        if str(pod.get("metadata", {}).get("name", "")).startswith(deployment_prefix)
        and not any(
            str(pod.get("metadata", {}).get("name", "")).startswith(excluded_prefix)
            for excluded_prefix in excluded_prefixes
        )
    ]


def collect_carts_state(namespace: str) -> dict:
    pods = collect_service_pods(namespace, "carts-")
    result = {"pods": [], "restart_total": 0}
    for pod in pods:
        pod_name = str(pod.get("metadata", {}).get("name", ""))
        phase = str(pod.get("status", {}).get("phase", ""))
        restart_count = 0
        waiting_reasons: list[str] = []
        last_terminated_reasons: list[str] = []
        ready = True
        for cs in pod.get("status", {}).get("containerStatuses", []) or []:
            restart_count += int(cs.get("restartCount", 0) or 0)
            ready = ready and bool(cs.get("ready", False))
            waiting = ((cs.get("state", {}) or {}).get("waiting", {}) or {})
            terminated = ((cs.get("lastState", {}) or {}).get("terminated", {}) or {})
            if waiting.get("reason"):
                waiting_reasons.append(str(waiting["reason"]))
            if terminated.get("reason"):
                last_terminated_reasons.append(str(terminated["reason"]))
        result["restart_total"] += restart_count
        result["pods"].append(
            {
                "name": pod_name,
                "phase": phase,
                "ready": ready,
                "restart_count": restart_count,
                "waiting_reasons": waiting_reasons,
                "last_terminated_reasons": last_terminated_reasons,
            }
        )
    return result


def collect_carts_logs(namespace: str, since_seconds: int) -> dict:
    output = {"since_seconds": since_seconds, "pods": [], "error_pattern_count": 0}
    for pod in collect_service_pods(namespace, "carts-"):
        pod_name = str(pod.get("metadata", {}).get("name", ""))
        try:
            text = kubectl_text(
                ["logs", pod_name, "-n", namespace, f"--since={max(1, since_seconds)}s"],
                timeout=60,
            )
        except Exception as exc:
            text = f"<log collection failed: {exc}>"
        matched_lines = []
        for line in text.splitlines():
            if any(pattern.search(line) for pattern in CARTS_LOG_IGNORE_PATTERNS):
                continue
            if any(pattern.search(line) for pattern in CARTS_LOG_PATTERNS):
                matched_lines.append(line[:500])
        output["error_pattern_count"] += len(matched_lines)
        output["pods"].append(
            {
                "name": pod_name,
                "matched_lines": matched_lines[:200],
                "matched_line_count": len(matched_lines),
            }
        )
    return output


def verify_stable_env(namespace: str, timeout_seconds: int) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        deploys = kubectl_json(["get", "deploy", "-n", namespace, "-o", "json"], timeout=30)
        pods = kubectl_json(["get", "pods", "-n", namespace, "-o", "json"], timeout=30)
        if not isinstance(deploys, dict) or not isinstance(pods, dict):
            time.sleep(3)
            continue
        deploy_map = {item["metadata"]["name"]: item for item in deploys.get("items", [])}
        ok = True
        for name in REQUIRED_DEPLOYS:
            item = deploy_map.get(name)
            if not item:
                ok = False
                break
            desired = int(item.get("spec", {}).get("replicas", 0) or 0)
            available = int(item.get("status", {}).get("availableReplicas", 0) or 0)
            updated = int(item.get("status", {}).get("updatedReplicas", 0) or 0)
            total = int(item.get("status", {}).get("replicas", 0) or 0)
            unavailable = int(item.get("status", {}).get("unavailableReplicas", 0) or 0)
            if available < desired or updated < desired or total != desired or unavailable > 0:
                ok = False
                break
        if ok:
            for pod in pods.get("items", []):
                pod_name = pod.get("metadata", {}).get("name", "")
                if not any(pod_name.startswith(prefix) for prefix in REQUIRED_DEPLOYS):
                    continue
                phase = str(pod.get("status", {}).get("phase", ""))
                if phase == "Pending":
                    ok = False
                    break
                for cs in pod.get("status", {}).get("containerStatuses", []) or []:
                    waiting = (cs.get("state", {}) or {}).get("waiting", {}) or {}
                    if waiting.get("reason") in {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull"}:
                        ok = False
                        break
                if not ok:
                    break
        if ok:
            first = collect_required_pod_restarts(namespace)
            time.sleep(15)
            second = collect_required_pod_restarts(namespace)
            if all(second.get(name, 0) <= count for name, count in first.items()):
                return
        time.sleep(5)
    raise SystemExit(f"environment not stable in {timeout_seconds}s")


def verify_manual_login(base_url: str, creds_file: Path, timeout: int = 15) -> None:
    lines = [line.strip() for line in creds_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        raise SystemExit("no credentials available for login verification")
    rec = json.loads(lines[0])
    token = base64.b64encode(f"{rec['username']}:{rec['password']}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/login",
        headers={"Authorization": f"Basic {token}", "User-Agent": "final-benchmark-login-check/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if int(response.status or 0) != 200:
                raise SystemExit(f"manual /login returned {int(response.status or 0)}")
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"manual /login returned {int(exc.code or 0)}") from exc


def sample_replicas(namespace: str, interval_seconds: float, stop_event: threading.Event, rows: list[dict]) -> None:
    started = time.time()
    while not stop_event.is_set():
        deploys = kubectl_json(["get", "deploy", "-n", namespace, "-o", "json"], timeout=20)
        now = time.time()
        sample = {
            "ts_unix_ms": int(now * 1000),
            "t_rel_s": round(now - started, 3),
        }
        if isinstance(deploys, dict):
            deploy_map = {item["metadata"]["name"]: item for item in deploys.get("items", [])}
            for name in TRACKED_DEPLOYS:
                item = deploy_map.get(name, {})
                sample[name] = {
                    "desired": int(item.get("spec", {}).get("replicas", 0) or 0),
                    "available": int(item.get("status", {}).get("availableReplicas", 0) or 0),
                }
        rows.append(sample)
        stop_event.wait(interval_seconds)


def build_replica_windows(samples: list[dict], total_duration: float, window_seconds: float) -> list[dict]:
    windows: list[dict] = []
    if total_duration <= 0:
        return windows
    count = int(math.ceil(total_duration / max(window_seconds, 1.0)))
    for idx in range(count):
        start_s = idx * window_seconds
        window_sample = None
        for sample in reversed(samples):
            if float(sample.get("t_rel_s", 0.0)) <= start_s + 0.001:
                window_sample = sample
                break
        if window_sample is None and samples:
            window_sample = samples[0]
        row = {
            "window_index": idx,
            "start_s": round(start_s, 3),
            "end_s": round(min(total_duration, start_s + window_seconds), 3),
        }
        for name in TRACKED_DEPLOYS:
            row[name] = {
                "desired": int((window_sample or {}).get(name, {}).get("desired", 0) or 0),
                "available": int((window_sample or {}).get(name, {}).get("available", 0) or 0),
            }
        windows.append(row)
    return windows


def violation_episode_count(client_windows: list[dict]) -> int:
    episodes = 0
    in_episode = False
    for window in client_windows:
        violating = bool(window.get("slo_violating", False))
        if violating and not in_episode:
            episodes += 1
            in_episode = True
        elif not violating:
            in_episode = False
    return episodes


def compute_replica_metrics(replica_windows: list[dict], window_seconds: float) -> dict:
    if not replica_windows:
        zero_map = {name: 0 for name in TRACKED_DEPLOYS}
        return {
            "window_count": 0,
            "peak_replicas": zero_map,
            "replica_growth": zero_map,
            "avg_replicas": {name: 0.0 for name in TRACKED_DEPLOYS},
            "replica_seconds": {name: 0.0 for name in TRACKED_DEPLOYS},
            "requested_cpu_core_minutes": {name: 0.0 for name in TRACKED_DEPLOYS},
            "overall_peak_replicas": 0,
            "overall_avg_replicas": 0.0,
            "total_replica_seconds": 0.0,
            "total_requested_cpu_core_minutes": 0.0,
        }

    peak = {name: 0 for name in TRACKED_DEPLOYS}
    first = replica_windows[0]
    last = replica_windows[-1]
    growth = {}
    replica_seconds = {name: 0.0 for name in TRACKED_DEPLOYS}
    cpu_core_minutes = {name: 0.0 for name in TRACKED_DEPLOYS}

    for name in TRACKED_DEPLOYS:
        growth[name] = int(last.get(name, {}).get("desired", 0) or 0) - int(first.get(name, {}).get("desired", 0) or 0)

    for row in replica_windows:
        total_window_replicas = 0
        for name in TRACKED_DEPLOYS:
            desired = int(row.get(name, {}).get("desired", 0) or 0)
            peak[name] = max(peak[name], desired)
            replica_seconds[name] += desired * window_seconds
            cpu_core_minutes[name] += (desired * CPU_REQUESTS[name] * window_seconds) / 60.0
            total_window_replicas += desired
        row["total_replicas"] = total_window_replicas

    total_duration = max(window_seconds * len(replica_windows), 0.001)
    avg = {name: round(replica_seconds[name] / total_duration, 3) for name in TRACKED_DEPLOYS}
    replica_seconds = {name: round(value, 3) for name, value in replica_seconds.items()}
    cpu_core_minutes = {name: round(value, 4) for name, value in cpu_core_minutes.items()}
    total_replica_seconds = round(sum(replica_seconds.values()), 3)
    total_requested_cpu_core_minutes = round(sum(cpu_core_minutes.values()), 4)
    overall_peak_replicas = max(sum(int(row.get(name, {}).get("desired", 0) or 0) for name in TRACKED_DEPLOYS) for row in replica_windows)
    overall_avg_replicas = round(total_replica_seconds / total_duration, 3)

    return {
        "window_count": len(replica_windows),
        "peak_replicas": peak,
        "replica_growth": growth,
        "avg_replicas": avg,
        "replica_seconds": replica_seconds,
        "requested_cpu_core_minutes": cpu_core_minutes,
        "overall_peak_replicas": overall_peak_replicas,
        "overall_avg_replicas": overall_avg_replicas,
        "total_replica_seconds": total_replica_seconds,
        "total_requested_cpu_core_minutes": total_requested_cpu_core_minutes,
    }


def compute_benchmark_metrics(client_windows: list[dict], replica_windows: list[dict], slo_ms: float, total_duration: float) -> dict:
    violating_windows = [w for w in client_windows if bool(w.get("slo_violating", False))]
    violation_time = round(sum(float(w["end_s"]) - float(w["start_s"]) for w in violating_windows), 3)
    violation_rate = round(violation_time / max(total_duration, 1.0), 4)
    episodes = violation_episode_count(client_windows)

    first_violation = violating_windows[0] if violating_windows else None
    first_violation_start = float(first_violation["start_s"]) if first_violation else None

    first_action_from_run_start = None
    for idx, row in enumerate(replica_windows):
        if idx == 0:
            continue
        increased = any(
            int(row.get(name, {}).get("desired", 0) or 0) > int(replica_windows[idx - 1].get(name, {}).get("desired", 0) or 0)
            for name in TRACKED_DEPLOYS
        )
        if increased:
            first_action_from_run_start = round(float(row["start_s"]), 3)
            break

    first_action_time = None
    if first_violation is not None:
        for idx, row in enumerate(replica_windows):
            if idx == 0 or float(row.get("start_s", 0.0)) < first_violation_start:
                continue
            increased = any(
                int(row.get(name, {}).get("desired", 0) or 0) > int(replica_windows[idx - 1].get(name, {}).get("desired", 0) or 0)
                for name in TRACKED_DEPLOYS
            )
            if increased:
                first_action_time = round(float(row["start_s"]) - first_violation_start, 3)
                break

    recovery_time = None
    if first_violation is not None:
        for idx in range(len(client_windows) - 1):
            row_a = client_windows[idx]
            row_b = client_windows[idx + 1]
            if float(row_a["start_s"]) < first_violation_start:
                continue
            both_ok = (
                not bool(row_a.get("slo_violating", False))
                and not bool(row_b.get("slo_violating", False))
                and float(row_a.get("p90_latency_ms", 0.0) or 0.0) <= slo_ms
                and float(row_b.get("p90_latency_ms", 0.0) or 0.0) <= slo_ms
            )
            if both_ok:
                recovery_time = round(float(row_a["start_s"]) - first_violation_start, 3)
                break

    return {
        "slo_violation_time_seconds": violation_time,
        "slo_violation_rate": violation_rate,
        "slo_violation_episode_count": episodes,
        "time_to_first_action_from_run_start_seconds": first_action_from_run_start,
        "time_to_first_action_seconds": first_action_time,
        "recovery_time_seconds": recovery_time,
    }


def write_markdown_summary(path: Path, arm: str, workload: list[dict], summary: dict) -> None:
    frontend = summary["frontend_client_metrics"]
    replicas = summary["replica_metrics"]
    benchmark = summary["benchmark_metrics"]
    carts = summary["carts_validity"]
    lines = [
        f"# {arm} Final Benchmark Summary",
        "",
        "## Workload",
        "",
        "```json",
        json.dumps(workload, indent=2, sort_keys=True),
        "```",
        "",
        "## Metrics",
        "",
        f"- SLO violation time: {benchmark['slo_violation_time_seconds']} s",
        f"- SLO violation rate: {benchmark['slo_violation_rate']}",
        f"- Time to first action from run start: {benchmark.get('time_to_first_action_from_run_start_seconds')}",
        f"- Time to first action: {benchmark['time_to_first_action_seconds']}",
        f"- Recovery time: {benchmark['recovery_time_seconds']}",
        f"- Error rate: {frontend['error_rate']}",
        f"- Peak replicas: {replicas['peak_replicas']}",
        f"- Average replicas: {replicas['avg_replicas']}",
        f"- Requested CPU core-minutes: {replicas['requested_cpu_core_minutes']}",
        f"- Total requested CPU core-minutes: {replicas['total_requested_cpu_core_minutes']}",
        f"- Replica-seconds: {replicas['replica_seconds']}",
        f"- Total replica-seconds: {replicas['total_replica_seconds']}",
        f"- Carts hidden failure indicator: {carts['hidden_failure_indicator']}",
        f"- Carts invalid run: {carts['run_invalid']}",
        f"- Carts restart delta: {carts['restart_delta']}",
        f"- Carts log error pattern count: {carts['log_error_pattern_count']}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a fixed final /login benchmark and summarize per-window client and replica metrics.")
    parser.add_argument("--namespace", default="sock-shop")
    parser.add_argument("--base-url", default="http://127.0.0.1:30001")
    parser.add_argument("--creds-file", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--phase-json", default="", help="JSON array of phases [{name,duration_seconds,rps},...]")
    parser.add_argument("--phases-file", type=Path, default=None, help="Path to JSON file containing phases")
    parser.add_argument("--slo-ms", type=float, default=200.0)
    parser.add_argument("--stable-timeout-seconds", type=int, default=600)
    parser.add_argument("--sample-interval-seconds", type=float, default=10.0)
    parser.add_argument("--window-seconds", type=float, default=10.0)
    parser.add_argument("--arm-name", default="benchmark")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    verify_stable_env(args.namespace, args.stable_timeout_seconds)
    verify_manual_login(args.base_url, args.creds_file)

    if args.phases_file:
        phases = json.loads(args.phases_file.read_text(encoding="utf-8"))
    elif args.phase_json:
        phases = json.loads(args.phase_json)
    else:
        raise SystemExit("Either --phase-json or --phases-file is required")

    phase_path = args.output_dir / "workload_profile.json"
    phase_path.write_text(json.dumps(phases, indent=2, sort_keys=True), encoding="utf-8")
    carts_before = collect_carts_state(args.namespace)

    samples: list[dict] = []
    stop_event = threading.Event()
    sampler = threading.Thread(
        target=sample_replicas,
        args=(args.namespace, args.sample_interval_seconds, stop_event, samples),
        daemon=True,
    )
    sampler.start()

    try:
        subprocess.check_call(
            [
                sys.executable,
                str(PROBE_SCRIPT),
                "--base-url",
                args.base_url,
                "--creds-file",
                str(args.creds_file),
                "--phases-file",
                str(phase_path),
                "--slo-ms",
                str(args.slo_ms),
                "--window-seconds",
                str(args.window_seconds),
                "--session-pool",
                "24",
                "--max-in-flight",
                "96",
                "--split-seconds",
                "999999",
                "--output-dir",
                str(args.output_dir),
            ]
        )
    finally:
        stop_event.set()
        sampler.join(timeout=10)

    probe_summary = json.loads((args.output_dir / "login_probe_summary.json").read_text(encoding="utf-8"))
    client_windows = json.loads((args.output_dir / "login_probe_windows.json").read_text(encoding="utf-8"))
    total_duration = float(probe_summary.get("duration_seconds", 0.0) or 0.0)
    carts_after = collect_carts_state(args.namespace)
    carts_logs = collect_carts_logs(args.namespace, max(120, int(total_duration) + 120))
    replica_windows = build_replica_windows(samples, total_duration, args.window_seconds)
    replica_metrics = compute_replica_metrics(replica_windows, args.window_seconds)
    benchmark_metrics = compute_benchmark_metrics(client_windows, replica_windows, args.slo_ms, total_duration)
    frontend_metrics = probe_summary["overall"]
    restart_delta = int(carts_after.get("restart_total", 0) or 0) - int(carts_before.get("restart_total", 0) or 0)
    hidden_failure_indicator = bool(
        (restart_delta > 0 or int(carts_logs.get("error_pattern_count", 0) or 0) > 0)
        and float(frontend_metrics.get("success_rate", 0.0) or 0.0) >= 0.95
    )
    run_invalid = bool(restart_delta > 0 or hidden_failure_indicator)
    carts_validity = {
        "restart_delta": restart_delta,
        "before": carts_before,
        "after": carts_after,
        "log_error_pattern_count": int(carts_logs.get("error_pattern_count", 0) or 0),
        "hidden_failure_indicator": hidden_failure_indicator,
        "run_invalid": run_invalid,
        "reason": (
            "hidden_carts_failure_detected"
            if hidden_failure_indicator
            else ("carts_restart_during_run" if restart_delta > 0 else "ok")
        ),
    }

    (args.output_dir / "replica_windows.json").write_text(json.dumps(replica_windows, indent=2, sort_keys=True), encoding="utf-8")
    (args.output_dir / "replica_samples.json").write_text(json.dumps(samples, indent=2, sort_keys=True), encoding="utf-8")
    (args.output_dir / "carts_state_before.json").write_text(json.dumps(carts_before, indent=2, sort_keys=True), encoding="utf-8")
    (args.output_dir / "carts_state_after.json").write_text(json.dumps(carts_after, indent=2, sort_keys=True), encoding="utf-8")
    (args.output_dir / "carts_failure_visibility.json").write_text(json.dumps(carts_logs, indent=2, sort_keys=True), encoding="utf-8")

    summary = {
        "arm": args.arm_name,
        "frontend_client_metrics": frontend_metrics,
        "frontend_slo_ms": args.slo_ms,
        "namespace": args.namespace,
        "base_url": args.base_url,
        "phases": phases,
        "replica_metrics": replica_metrics,
        "benchmark_metrics": benchmark_metrics,
        "carts_validity": carts_validity,
        "workload_fallback_recommended": bool(probe_summary.get("immediate_collapse", False)),
        "client_windows": client_windows,
        "replica_windows": replica_windows,
    }
    (args.output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    write_markdown_summary(args.output_dir / "summary.md", args.arm_name, phases, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
