#!/usr/bin/env python3
import argparse
import json
import random
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from statistics import quantiles

SCRIPT_DIR = Path(__file__).resolve().parent
COMMON_DIR = SCRIPT_DIR.parent / "common"
COLLECT_SCRIPT = COMMON_DIR / "collect_metrics.py"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def choose_route(route_mix: dict) -> str:
    names = list(route_mix.keys())
    weights = [float(route_mix[name]) for name in names]
    return random.choices(names, weights=weights, k=1)[0]


def make_request(opener, base_url: str, route_cfg: dict) -> dict:
    method = str(route_cfg.get("method", "GET")).upper()
    path = str(route_cfg.get("path", "/"))
    url = urllib.parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    body = route_cfg.get("body")
    headers = {"User-Agent": "functional-eval-runner/1.0"}
    headers.update(route_cfg.get("headers", {}))

    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")

    request = urllib.request.Request(url=url, data=data, headers=headers, method=method)
    started = time.time()
    status = 0
    success = False
    error_text = ""
    response_bytes = 0
    try:
        with opener.open(request, timeout=float(route_cfg.get("timeout_seconds", 10))) as response:
            payload = response.read()
            status = int(response.getcode() or 0)
            response_bytes = len(payload)
            success = 200 <= status < 400
    except urllib.error.HTTPError as exc:
        status = int(exc.code or 0)
        response_bytes = len(exc.read() or b"")
        error_text = str(exc)
    except Exception as exc:
        error_text = str(exc)

    ended = time.time()
    return {
        "ts_unix_ms": int(started * 1000),
        "route": str(route_cfg.get("name", path)),
        "method": method,
        "path": path,
        "status": status,
        "success": success,
        "latency_ms": round((ended - started) * 1000.0, 3),
        "response_bytes": response_bytes,
        "error": error_text,
    }


def make_flow_request(opener, base_url: str, route_cfg: dict) -> dict:
    steps = route_cfg.get("steps", [])
    if not steps:
        return make_request(opener, base_url, route_cfg)

    started = time.time()
    final_row = None
    all_steps = []
    for index, step_cfg in enumerate(steps):
        merged = dict(step_cfg)
        merged.setdefault("name", f"{route_cfg.get('name', 'flow')}#{index + 1}")
        row = make_request(opener, base_url, merged)
        row["step_index"] = index + 1
        row["step_name"] = merged["name"]
        all_steps.append(row)
        final_row = row
        if not row.get("success"):
            break

    ended = time.time()
    final_row = final_row or {
        "ts_unix_ms": int(started * 1000),
        "status": 0,
        "success": False,
        "latency_ms": 0.0,
        "response_bytes": 0,
        "error": "empty_flow",
        "method": "FLOW",
        "path": str(route_cfg.get("path", "")),
    }
    return {
        "ts_unix_ms": int(started * 1000),
        "route": str(route_cfg.get("name", route_cfg.get("path", "/"))),
        "method": "FLOW",
        "path": str(route_cfg.get("path", final_row.get("path", ""))),
        "status": int(final_row.get("status", 0) or 0),
        "success": bool(all(step.get("success") for step in all_steps)) if all_steps else False,
        "latency_ms": round((ended - started) * 1000.0, 3),
        "response_bytes": int(sum(int(step.get("response_bytes", 0) or 0) for step in all_steps)),
        "error": next((str(step.get("error", "")) for step in all_steps if step.get("error")), ""),
        "steps": all_steps,
    }


def p90(values: list[float]) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    return float(quantiles(values, n=10, method="inclusive")[8])


def wait_for_collection_start(case_dir: Path, timeout_seconds: int = 180) -> None:
    session_path = case_dir / "collector_session.json"
    deadline = time.time() + max(10, timeout_seconds)
    while time.time() < deadline:
        if session_path.exists():
            try:
                session = load_json(session_path)
                if int(session.get("collection_started_at_unix_ms", 0) or 0) > 0:
                    return
            except Exception:
                pass
        time.sleep(1)
    raise TimeoutError(f"collector did not start in time for {case_dir.name}")


def print_run_summary(summary: dict) -> None:
    route_counts = summary.get("route_counts", {}) or {}
    route_parts = ", ".join(f"{name}={count}" for name, count in sorted(route_counts.items()))
    print(f"Case: {summary.get('case_name', '-')}")
    print(
        "Client: "
        f"p90={float(summary.get('client_p90_latency_ms', 0.0)):.3f} ms, "
        f"success_rps={float(summary.get('success_rps', 0.0)):.3f}, "
        f"duration={float(summary.get('duration_seconds', 0.0)):.3f} s"
    )
    print(
        "Requests: "
        f"sent={int(summary.get('sent_requests', 0))}, "
        f"ok={int(summary.get('successful_responses', 0))}, "
        f"failed={int(summary.get('failed_responses', 0))}"
    )
    print(f"Config: {summary.get('config_path', '-')}")
    if route_parts:
        print(f"Routes: {route_parts}")


def run_case(
    config_path: Path,
    output_dir: Path,
    aggregator_base_url: str,
    namespace: str,
    mode: str,
    interval_seconds: int,
    stabilization_seconds: int,
    prepare_timeout_seconds: int,
    skip_prepare: bool,
) -> int:
    config = load_json(config_path)
    case_name = str(config.get("case_name") or config_path.stem)
    case_dir = output_dir / case_name
    ensure_dir(case_dir)

    route_defs = config.get("routes", {})
    phases = config.get("phases", [])
    if not route_defs or not phases:
        raise ValueError("config must define routes and phases")

    cookie_jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    base_url = str(config["base_url"])
    random.seed(int(config.get("random_seed", 7)))

    request_log_path = case_dir / "request_log.ndjson"
    summary_path = case_dir / "request_summary.json"

    totals = {
        "case_name": case_name,
        "base_url": base_url,
        "config_path": str(config_path),
        "started_at_unix_ms": int(time.time() * 1000),
        "sent_requests": 0,
        "successful_responses": 0,
        "failed_responses": 0,
        "duration_seconds": 0.0,
        "route_counts": {},
    }

    collector_duration = int(
        max(
            sum(float(phase.get("duration_seconds", 60)) for phase in phases),
            config.get("collector_duration_seconds", 0),
        )
    )
    collector_cmd = [
        sys.executable,
        str(COLLECT_SCRIPT),
        "--case-name",
        case_name,
        "--output-root",
        str(output_dir),
        "--aggregator-base-url",
        aggregator_base_url,
        "--namespace",
        namespace,
        "--duration-seconds",
        str(max(1, collector_duration)),
        "--interval-seconds",
        str(max(1, interval_seconds)),
        "--case-config",
        str(config_path),
        "--mode",
        mode,
        "--stabilization-seconds",
        str(max(0, stabilization_seconds)),
        "--prepare-timeout-seconds",
        str(max(10, prepare_timeout_seconds)),
    ]
    if skip_prepare:
        collector_cmd.append("--skip-prepare")

    collector_proc = subprocess.Popen(collector_cmd)
    start_time = time.time()
    try:
        wait_for_collection_start(case_dir, timeout_seconds=stabilization_seconds + prepare_timeout_seconds + 60)
        with request_log_path.open("w", encoding="utf-8") as handle:
            for phase in phases:
                phase_name = str(phase.get("name", "phase"))
                phase_duration = float(phase.get("duration_seconds", 60))
                phase_rps = max(0.01, float(phase.get("rps", 1.0)))
                interval = 1.0 / phase_rps
                phase_end = time.time() + phase_duration

                while time.time() < phase_end:
                    route_name = choose_route(phase["route_mix"])
                    route_cfg = dict(route_defs[route_name])
                    route_cfg["name"] = route_name
                    route_opener = opener
                    if route_cfg.get("isolated_session"):
                        route_opener = urllib.request.build_opener(
                            urllib.request.HTTPCookieProcessor(CookieJar())
                        )

                    row = make_flow_request(route_opener, base_url, route_cfg)
                    row["phase"] = phase_name
                    handle.write(json.dumps(row, separators=(",", ":")) + "\n")
                    handle.flush()

                    totals["sent_requests"] += 1
                    totals["successful_responses"] += 1 if row["success"] else 0
                    totals["failed_responses"] += 0 if row["success"] else 1
                    totals["route_counts"][route_name] = totals["route_counts"].get(route_name, 0) + 1

                    elapsed = time.time() - (row["ts_unix_ms"] / 1000.0)
                    sleep_for = max(0.0, interval - elapsed)
                    time.sleep(sleep_for)
    finally:
        collector_rc = collector_proc.wait()
        if collector_rc != 0:
            raise RuntimeError(f"collector failed with exit code {collector_rc}")

    totals["duration_seconds"] = round(time.time() - start_time, 3)

    latencies = []
    with request_log_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if row.get("success"):
                latencies.append(float(row.get("latency_ms", 0.0)))

    totals["success_rps"] = round(
        totals["successful_responses"] / max(totals["duration_seconds"], 0.001), 3
    )
    totals["client_p90_latency_ms"] = round(p90(latencies), 3)
    totals["ended_at_unix_ms"] = int(time.time() * 1000)

    summary_path.write_text(json.dumps(totals, indent=2, sort_keys=True), encoding="utf-8")
    print_run_summary(totals)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a low-rate functional Sock Shop case.")
    parser.add_argument("--config", required=True, type=Path, help="Path to case config JSON")
    parser.add_argument(
        "--output-root",
        default=Path("results/metrics"),
        type=Path,
        help="Directory where case output folders are written",
    )
    parser.add_argument(
        "--aggregator-base-url",
        default="http://127.0.0.1:30938",
        help="Base URL for the ThriveScale aggregator",
    )
    parser.add_argument(
        "--namespace",
        default="sock-shop",
        help="Kubernetes namespace for deployment replica collection",
    )
    parser.add_argument(
        "--mode",
        choices=["observation", "control"],
        default="observation",
        help="Observation disables autoscaling by scaling the controller to zero replicas",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=5,
        help="Collector polling interval in seconds",
    )
    parser.add_argument(
        "--stabilization-seconds",
        type=int,
        default=0,
        help="Settle time after reset and replica restore before collection begins",
    )
    parser.add_argument(
        "--prepare-timeout-seconds",
        type=int,
        default=15,
        help="Maximum time to wait for restored replicas before collection",
    )
    parser.add_argument(
        "--skip-prepare",
        action="store_true",
        default=True,
        help="Skip reset/warmup so metric and path checks finish in about one minute",
    )
    args = parser.parse_args()
    return run_case(
        args.config,
        args.output_root,
        args.aggregator_base_url,
        args.namespace,
        args.mode,
        args.interval_seconds,
        args.stabilization_seconds,
        args.prepare_timeout_seconds,
        args.skip_prepare,
    )


if __name__ == "__main__":
    sys.exit(main())
