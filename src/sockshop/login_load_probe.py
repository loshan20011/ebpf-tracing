#!/usr/bin/env python3
import argparse
import asyncio
import base64
import json
import math
import statistics
import time
import urllib.parse
from pathlib import Path

import aiohttp


def p90(values: list[float]) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(0.9 * (len(ordered) - 1))))
    return float(ordered[idx])


def summarize(rows: list[dict], slo_ms: float) -> dict:
    ok = [r for r in rows if r.get("success")]
    lats = [float(r.get("latency_ms", 0.0) or 0.0) for r in ok]
    violated = [x for x in lats if x > slo_ms]
    sent = len(rows)
    successful = len(ok)
    failed = sent - successful
    first_t = float(rows[0]["t_rel_s"]) if rows else 0.0
    last_t = float(rows[-1]["t_rel_s"]) if rows else 0.0
    elapsed = max(last_t - first_t, 0.001) if rows else 0.0
    return {
        "sent": sent,
        "successful": successful,
        "failed": failed,
        "success_rate": round(successful / max(sent, 1), 4),
        "error_rate": round(failed / max(sent, 1), 4),
        "success_rps": round(successful / max(elapsed, 0.001), 3) if rows else 0.0,
        "avg_latency_ms": round(statistics.fmean(lats), 3) if lats else 0.0,
        "p90_latency_ms": round(p90(lats), 3) if lats else 0.0,
        "slo_violations": len(violated),
        "slo_violation_rate": round(len(violated) / max(successful, 1), 4),
    }


def summarize_window(rows: list[dict], slo_ms: float, window_index: int, start_s: float, end_s: float) -> dict:
    stats = summarize(rows, slo_ms)
    return {
        "window_index": window_index,
        "start_s": round(start_s, 3),
        "end_s": round(end_s, 3),
        "sent": stats["sent"],
        "successful": stats["successful"],
        "failed": stats["failed"],
        "success_rate": stats["success_rate"],
        "error_rate": stats["error_rate"],
        "success_rps": stats["success_rps"],
        "avg_latency_ms": stats["avg_latency_ms"],
        "p90_latency_ms": stats["p90_latency_ms"],
        "slo_violation_rate": stats["slo_violation_rate"],
        "slo_violating": bool(stats["p90_latency_ms"] > slo_ms and stats["successful"] > 0),
    }


def build_windows(rows: list[dict], slo_ms: float, total_duration_s: float, window_seconds: float) -> list[dict]:
    if total_duration_s <= 0:
        return []
    windows: list[dict] = []
    count = int(math.ceil(total_duration_s / max(window_seconds, 1.0)))
    for idx in range(count):
        start_s = idx * window_seconds
        end_s = min(total_duration_s, start_s + window_seconds)
        window_rows = [r for r in rows if start_s <= float(r.get("t_rel_s", 0.0)) < end_s]
        windows.append(summarize_window(window_rows, slo_ms, idx, start_s, end_s))
    return windows


async def do_login(session: aiohttp.ClientSession, base_url: str, auth_header: str) -> dict:
    started = time.time()
    status = 0
    success = False
    error = ""
    body_text = ""
    try:
        async with session.get(
            urllib.parse.urljoin(base_url.rstrip("/") + "/", "login"),
            headers={"Authorization": auth_header, "User-Agent": "sockshop-login-probe/1.0"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            status = int(resp.status or 0)
            body_text = await resp.text()
            success = 200 <= status < 400
    except asyncio.TimeoutError:
        error = "timeout"
    except Exception as exc:
        error = str(exc)
    ended = time.time()
    return {
        "ts_unix_ms": int(started * 1000),
        "latency_ms": round((ended - started) * 1000.0, 3),
        "status": status,
        "success": success,
        "error": error,
        "body": body_text[:200],
    }


async def main_async(args) -> int:
    creds_records = []
    for line in Path(args.creds_file).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        username = rec["username"]
        password = rec["password"]
        auth = base64.b64encode(f"{username}:{password}".encode()).decode()
        creds_records.append(f"Basic {auth}")
    if not creds_records:
        raise SystemExit("No credentials found")

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "login_probe.ndjson"
    summary_path = out_dir / "login_probe_summary.json"
    windows_path = out_dir / "login_probe_windows.json"

    sessions = [
        aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar())
        for _ in range(max(1, args.session_pool))
    ]
    max_in_flight = max(1, args.max_in_flight)
    sem = asyncio.Semaphore(max_in_flight)
    active_tasks: set[asyncio.Task] = set()
    rows: list[dict] = []
    recent_rows: list[dict] = []
    recent_window_s = float(args.print_window_seconds)
    write_lock = asyncio.Lock()
    phases = []
    if args.phases_file:
        phases = json.loads(Path(args.phases_file).read_text(encoding="utf-8"))
    elif args.phase_json:
        phases = json.loads(args.phase_json)
    else:
        phases = [{"name": "steady", "duration_seconds": args.duration_seconds, "rps": args.rps}]

    total_duration_s = sum(float(phase.get("duration_seconds", 0) or 0) for phase in phases)
    start = time.time()
    session_index = 0
    creds_index = 0
    last_print = time.monotonic()

    async def record(row: dict) -> None:
        nonlocal recent_rows, last_print
        async with write_lock:
            rows.append(row)
            recent_rows.append(row)
            cutoff = time.time() - recent_window_s
            recent_rows = [r for r in recent_rows if r["ts_unix_ms"] >= int(cutoff * 1000)]
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, separators=(",", ":")) + "\n")
            now = time.monotonic()
            if now - last_print >= 1.0:
                stats = summarize(recent_rows, args.slo_ms)
                print(
                    f"[login] phase={row.get('phase', 'steady')} target_rps={row.get('target_rps', args.rps):.1f} "
                    f"sent={stats['sent']} ok={stats['successful']} err={stats['failed']} "
                    f"rps={stats['success_rps']:.1f} avg_ms={stats['avg_latency_ms']:.1f} "
                    f"p90_ms={stats['p90_latency_ms']:.1f} viol_rate={stats['slo_violation_rate']:.3f} "
                    f"in_flight={len(active_tasks)}",
                    flush=True,
                )
                last_print = now

    async def fire(session: aiohttp.ClientSession, auth_header: str, phase_name: str, phase_rps: float) -> None:
        async with sem:
            row = await do_login(session, args.base_url, auth_header)
            row["t_rel_s"] = round(time.time() - start, 3)
            row["phase"] = phase_name
            row["target_rps"] = float(phase_rps)
            await record(row)

    log_path.write_text("", encoding="utf-8")
    try:
        for phase in phases:
            phase_name = str(phase.get("name", "phase"))
            phase_rps = float(phase.get("rps", args.rps) or args.rps)
            phase_end = time.monotonic() + float(phase.get("duration_seconds", 0) or 0)
            next_emit = time.monotonic()
            while time.monotonic() < phase_end:
                now = time.monotonic()
                if now < next_emit:
                    await asyncio.sleep(min(0.005, next_emit - now))
                    continue
                while len(active_tasks) >= max_in_flight:
                    await asyncio.sleep(0.005)
                sess = sessions[session_index % len(sessions)]
                session_index += 1
                auth_header = creds_records[creds_index % len(creds_records)]
                creds_index += 1
                task = asyncio.create_task(fire(sess, auth_header, phase_name, phase_rps))
                active_tasks.add(task)
                task.add_done_callback(active_tasks.discard)
                next_emit += 1.0 / max(phase_rps, 0.01)
        if active_tasks:
            await asyncio.gather(*list(active_tasks))
    finally:
        for s in sessions:
            await s.close()

    split_s = args.split_seconds
    before = [r for r in rows if r["t_rel_s"] < split_s]
    after = [r for r in rows if r["t_rel_s"] >= split_s]
    windows = build_windows(rows, args.slo_ms, total_duration_s, float(args.window_seconds))
    first_phase_seconds = float(phases[0].get("duration_seconds", 0) or 0) if phases else 0.0
    first_phase_windows = [w for w in windows if float(w["start_s"]) < first_phase_seconds]
    immediate_collapse = bool(
        first_phase_windows
        and all(
            (
                float(w["error_rate"]) >= 0.50
                or float(w["p90_latency_ms"]) >= max(args.slo_ms * 3.0, 600.0)
            )
            for w in first_phase_windows[: min(2, len(first_phase_windows))]
        )
    )
    summary = {
        "base_url": args.base_url,
        "duration_seconds": total_duration_s,
        "rps_target": args.rps,
        "frontend_slo_ms": args.slo_ms,
        "split_seconds": split_s,
        "window_seconds": float(args.window_seconds),
        "overall": summarize(rows, args.slo_ms),
        "before_split": summarize(before, args.slo_ms),
        "after_split": summarize(after, args.slo_ms),
        "windows": windows,
        "immediate_collapse": immediate_collapse,
    }
    windows_path.write_text(json.dumps(windows, indent=2, sort_keys=True), encoding="utf-8")
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run fixed-rate Sock Shop /login probe with seeded users.")
    parser.add_argument("--base-url", default="http://127.0.0.1:30001")
    parser.add_argument("--creds-file", required=True)
    parser.add_argument("--rps", type=float, default=200.0)
    parser.add_argument("--duration-seconds", type=int, default=120)
    parser.add_argument("--phases-file", default="")
    parser.add_argument("--phase-json", default="")
    parser.add_argument("--split-seconds", type=int, default=60)
    parser.add_argument("--slo-ms", type=float, default=200.0)
    parser.add_argument("--window-seconds", type=float, default=10.0)
    parser.add_argument("--print-window-seconds", type=float, default=5.0)
    parser.add_argument("--session-pool", type=int, default=64)
    parser.add_argument("--max-in-flight", type=int, default=1000)
    parser.add_argument("--output-dir", default="results/login-probe")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
