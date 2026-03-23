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
COLLECT_SCRIPT = SCRIPT_DIR / "collect_metrics.py"
SUMMARIZE_SCRIPT = SCRIPT_DIR / "summarize_bottleneck_case.py"


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
    headers = {"User-Agent": "bottleneck-eval-runner/1.0"}
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


def run_traffic(config: dict, case_dir: Path) -> None:
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
        "case_name": str(config.get("case_name", case_dir.name)),
        "base_url": base_url,
        "sent_requests": 0,
        "successful_responses": 0,
        "failed_responses": 0,
        "duration_seconds": 0.0,
        "route_counts": {},
        "started_at_unix_ms": int(time.time() * 1000),
    }

    start_time = time.time()
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

                row = make_request(opener, base_url, route_cfg)
                row["phase"] = phase_name
                handle.write(json.dumps(row, separators=(",", ":")) + "\n")
                handle.flush()

                totals["sent_requests"] += 1
                totals["successful_responses"] += 1 if row["success"] else 0
                totals["failed_responses"] += 0 if row["success"] else 1
                totals["route_counts"][route_name] = totals["route_counts"].get(route_name, 0) + 1

                elapsed = time.time() - (row["ts_unix_ms"] / 1000.0)
                time.sleep(max(0.0, interval - elapsed))

    totals["duration_seconds"] = round(time.time() - start_time, 3)
    totals["ended_at_unix_ms"] = int(time.time() * 1000)

    latencies = []
    with request_log_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("success"):
                latencies.append(float(row.get("latency_ms", 0.0)))

    totals["success_rps"] = round(
        totals["successful_responses"] / max(totals["duration_seconds"], 0.001), 3
    )
    totals["client_p90_latency_ms"] = round(p90(latencies), 3)
    summary_path.write_text(json.dumps(totals, indent=2, sort_keys=True), encoding="utf-8")


def run_case(case_config: Path, output_root: Path, aggregator_base_url: str, mode_override: str | None) -> int:
    config = load_json(case_config)
    case_name = str(config.get("case_name") or case_config.stem)
    phase_name = str(config.get("phase_name", "bottleneck_service"))
    case_dir = output_root / case_name
    ensure_dir(case_dir)

    (case_dir / "case_config.json").write_text(
        json.dumps(config, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    collector_duration = int(config.get("collector_duration_seconds", config.get("duration_seconds", 60)))
    mode = str(mode_override or config.get("mode", "observation"))
    namespace = str(config.get("namespace", "thrive-demo"))
    system_namespace = str(config.get("system_namespace", "thrive-scale"))
    controller_deployment = str(config.get("controller_deployment", "custom-autoscaler"))
    stabilization_seconds = int(config.get("stabilization_seconds", 20))
    interval_seconds = int(config.get("interval_seconds", 5))

    collector_cmd = [
        sys.executable,
        str(COLLECT_SCRIPT),
        "--case-name",
        case_name,
        "--output-root",
        str(output_root),
        "--aggregator-base-url",
        aggregator_base_url,
        "--namespace",
        namespace,
        "--duration-seconds",
        str(collector_duration),
        "--interval-seconds",
        str(interval_seconds),
        "--case-config",
        str(case_config),
        "--mode",
        mode,
        "--system-namespace",
        system_namespace,
        "--controller-deployment",
        controller_deployment,
        "--stabilization-seconds",
        str(stabilization_seconds),
    ]

    collector_proc = subprocess.Popen(collector_cmd)
    try:
        wait_for_collection_start(case_dir, timeout_seconds=stabilization_seconds + 180)
        run_traffic(config, case_dir)
    finally:
        collector_rc = collector_proc.wait()
        if collector_rc != 0:
            raise RuntimeError(f"collector failed with exit code {collector_rc}")

    summarize_cmd = [
        sys.executable,
        str(SUMMARIZE_SCRIPT),
        "--case-dir",
        str(case_dir),
        "--phase-name",
        phase_name,
    ]
    subprocess.check_call(summarize_cmd)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one bottleneck evaluation case end-to-end.")
    parser.add_argument("--case-config", required=True, type=Path, help="Case config JSON")
    parser.add_argument("--output-root", required=True, type=Path, help="Root output directory for this phase")
    parser.add_argument(
        "--aggregator-base-url",
        default="http://127.0.0.1:30938",
        help="Base URL for the ThriveScale aggregator",
    )
    parser.add_argument("--mode", choices=["observation", "control"], help="Optional mode override")
    args = parser.parse_args()
    return run_case(args.case_config, args.output_root, args.aggregator_base_url, args.mode)


if __name__ == "__main__":
    sys.exit(main())
