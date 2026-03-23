#!/usr/bin/env python3
import argparse
import asyncio
import json
import random
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from statistics import quantiles

try:
    import aiohttp
except ImportError as exc:
    raise SystemExit("Missing dependency 'aiohttp'. Install with: pip3 install aiohttp") from exc


SCRIPT_DIR = Path(__file__).resolve().parent
COLLECT_SCRIPT = SCRIPT_DIR.parent / "common" / "collect_metrics.py"
SUMMARIZE_SCRIPT = SCRIPT_DIR / "summarize_control_loop_case.py"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def choose_route(route_mix: dict) -> str:
    names = list(route_mix.keys())
    weights = [float(route_mix[name]) for name in names]
    return random.choices(names, weights=weights, k=1)[0]


def summarize_request_window(rows: list[dict], window_seconds: float) -> dict:
    if not rows:
        return {
            "sent": 0,
            "successful": 0,
            "failed": 0,
            "achieved_rps": 0.0,
            "avg_latency_ms": 0.0,
            "p90_latency_ms": 0.0,
        }
    successful = [row for row in rows if row.get("success")]
    latencies = [float(row.get("latency_ms", 0.0) or 0.0) for row in successful]
    return {
        "sent": len(rows),
        "successful": len(successful),
        "failed": len(rows) - len(successful),
        "achieved_rps": round(len(successful) / max(window_seconds, 0.001), 3),
        "avg_latency_ms": round(sum(latencies) / len(latencies), 3) if latencies else 0.0,
        "p90_latency_ms": round(p90(latencies), 3) if latencies else 0.0,
    }


async def make_request(session, base_url: str, route_cfg: dict) -> dict:
    method = str(route_cfg.get("method", "GET")).upper()
    path = str(route_cfg.get("path", "/"))
    url = urllib.parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    body = route_cfg.get("body")
    headers = {"User-Agent": "control-loop-eval-runner/1.0"}
    headers.update(route_cfg.get("headers", {}))

    request_kwargs = {
        "headers": headers,
        "timeout": aiohttp.ClientTimeout(total=float(route_cfg.get("timeout_seconds", 10))),
    }
    if body is not None:
        request_kwargs["json"] = body
        request_kwargs["headers"].setdefault("Content-Type", "application/json")

    started = time.time()
    status = 0
    success = False
    error_text = ""
    response_bytes = 0
    try:
        async with session.request(method, url, **request_kwargs) as response:
            payload = await response.read()
            status = int(response.status or 0)
            response_bytes = len(payload)
            success = 200 <= status < 400
    except aiohttp.ClientResponseError as exc:
        status = int(exc.status or 0)
        error_text = str(exc)
    except asyncio.TimeoutError:
        error_text = "timeout"
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


async def make_flow_request(session, base_url: str, route_cfg: dict) -> dict:
    steps = route_cfg.get("steps", [])
    if not steps:
        return await make_request(session, base_url, route_cfg)

    started = time.time()
    final_row = None
    all_steps = []
    for index, step_cfg in enumerate(steps):
        merged = dict(step_cfg)
        merged.setdefault("name", f"{route_cfg.get('name', 'flow')}#{index + 1}")
        row = await make_request(session, base_url, merged)
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


async def run_traffic_async(config: dict, case_dir: Path) -> None:
    route_defs = config.get("routes", {})
    phases = config.get("phases", [])
    if not route_defs or not phases:
        raise ValueError("config must define routes and phases")

    base_url = str(config["base_url"])
    random.seed(int(config.get("random_seed", 7)))
    max_in_flight = int(config.get("max_in_flight", 1000) or 1000)
    session_pool = int(config.get("session_pool", 32) or 32)
    stats_window_seconds = float(config.get("stats_window_seconds", 5.0) or 5.0)

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
    active_tasks: set[asyncio.Task] = set()
    sem = asyncio.Semaphore(max_in_flight)
    write_lock = asyncio.Lock()
    recent_rows: list[dict] = []
    window_start_monotonic = time.monotonic()

    async def emit_row(handle, row: dict, route_name: str) -> None:
        nonlocal recent_rows, window_start_monotonic
        async with write_lock:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")
            handle.flush()
            totals["sent_requests"] += 1
            totals["successful_responses"] += 1 if row["success"] else 0
            totals["failed_responses"] += 0 if row["success"] else 1
            totals["route_counts"][route_name] = totals["route_counts"].get(route_name, 0) + 1
            recent_rows.append(row)
            now_mono = time.monotonic()
            cutoff_ts_ms = int((time.time() - stats_window_seconds) * 1000)
            recent_rows = [item for item in recent_rows if int(item.get("ts_unix_ms", 0) or 0) >= cutoff_ts_ms]
            if (now_mono - window_start_monotonic) >= 1.0:
                stats = summarize_request_window(recent_rows, stats_window_seconds)
                print(
                    (
                        f"[traffic] phase={row['phase']} target_rps={row['target_rps']:.1f} "
                        f"sent={stats['sent']} ok={stats['successful']} err={stats['failed']} "
                        f"achieved_rps={stats['achieved_rps']:.1f} avg_ms={stats['avg_latency_ms']:.2f} "
                        f"p90_ms={stats['p90_latency_ms']:.2f} in_flight={len(active_tasks)}"
                    ),
                    flush=True,
                )
                window_start_monotonic = now_mono

    async def fire_one(handle, session, route_name: str, phase_name: str, target_rps: float) -> None:
        async with sem:
            route_cfg = dict(route_defs[route_name])
            route_cfg["name"] = route_name
            request_session = session
            temp_session = None
            if route_cfg.get("isolated_session"):
                temp_session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar())
                request_session = temp_session
            try:
                row = await make_flow_request(request_session, base_url, route_cfg)
            finally:
                if temp_session is not None:
                    await temp_session.close()
            row["phase"] = phase_name
            row["target_rps"] = float(target_rps)
            await emit_row(handle, row, route_name)

    connector = aiohttp.TCPConnector(limit=0)
    sessions = [
        aiohttp.ClientSession(
            connector=connector,
            connector_owner=False,
            cookie_jar=aiohttp.CookieJar(),
        )
        for _ in range(session_pool)
    ]
    session_index = 0

    # Truncate/create file before concurrent appends and keep one handle open.
    request_log_path.write_text("", encoding="utf-8")
    try:
        with request_log_path.open("a", encoding="utf-8") as log_handle:
            for phase in phases:
                phase_name = str(phase.get("name", "phase"))
                phase_duration = float(phase.get("duration_seconds", 60))
                phase_rps = max(0.01, float(phase.get("rps", 1.0)))
                phase_start = time.monotonic()
                phase_end = phase_start + phase_duration
                next_request_at = phase_start

                while True:
                    now_mono = time.monotonic()
                    if now_mono >= phase_end:
                        break
                    if now_mono < next_request_at:
                        await asyncio.sleep(min(0.01, max(0.0, next_request_at - now_mono)))
                        continue

                    route_name = choose_route(phase["route_mix"])
                    session = sessions[session_index % len(sessions)]
                    session_index += 1
                    task = asyncio.create_task(fire_one(log_handle, session, route_name, phase_name, phase_rps))
                    active_tasks.add(task)
                    task.add_done_callback(active_tasks.discard)
                    next_request_at += 1.0 / phase_rps

            if active_tasks:
                await asyncio.gather(*list(active_tasks))
    finally:
        for session in sessions:
            await session.close()
        await connector.close()

    totals["duration_seconds"] = round(time.time() - start_time, 3)
    totals["ended_at_unix_ms"] = int(time.time() * 1000)

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
    summary_path.write_text(json.dumps(totals, indent=2, sort_keys=True), encoding="utf-8")


def run_traffic(config: dict, case_dir: Path) -> None:
    asyncio.run(run_traffic_async(config, case_dir))


def run_case(case_config: Path, output_root: Path, aggregator_base_url: str, mode_override: str | None) -> int:
    config = load_json(case_config)
    case_name = str(config.get("case_name") or case_config.stem)
    phase_name = str(config.get("phase_name", "control_loop"))
    case_dir = output_root / case_name
    ensure_dir(case_dir)

    (case_dir / "case_config.json").write_text(
        json.dumps(config, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    collector_duration = int(config.get("collector_duration_seconds", config.get("duration_seconds", 60)))
    mode = str(mode_override or config.get("mode", "control"))
    namespace = str(config.get("namespace", "thrive-demo"))
    system_namespace = str(config.get("system_namespace", "thrive-scale"))
    controller_deployment = str(config.get("controller_deployment", "custom-autoscaler"))
    stabilization_seconds = int(config.get("stabilization_seconds", 35))
    interval_seconds = int(config.get("interval_seconds", 5))
    collection_start_timeout_seconds = int(
        config.get(
            "collection_start_timeout_seconds",
            max(stabilization_seconds + 420, collector_duration + 60),
        )
    )

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
        wait_for_collection_start(case_dir, timeout_seconds=collection_start_timeout_seconds)
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
    parser = argparse.ArgumentParser(description="Run one control-loop evaluation case end-to-end.")
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
