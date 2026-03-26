#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_ndjson(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def extract_replica_series(replica_rows: list[dict], deployments: list[str]) -> list[dict]:
    series = []
    for row in replica_rows:
        payload = row.get("payload") or {}
        items = payload.get("items", []) if isinstance(payload, dict) else []
        mapping = {}
        for item in items:
            name = str(item.get("metadata", {}).get("name", ""))
            if name not in deployments:
                continue
            mapping[name] = int(item.get("spec", {}).get("replicas", 0) or 0)
        if mapping:
            series.append({"ts_unix_ms": int(row.get("ts_unix_ms", 0) or 0), "replicas": mapping})
    return series


def first_scale_action(series: list[dict], initial: dict[str, int], start_ms: int) -> dict:
    for point in series:
        ts_ms = int(point.get("ts_unix_ms", 0) or 0)
        if ts_ms < start_ms:
            continue
        scaled = sorted(
            name for name, replicas in point.get("replicas", {}).items() if int(replicas) > int(initial.get(name, 0))
        )
        if scaled:
            return {
                "ts_unix_ms": ts_ms,
                "services": scaled,
                "seconds_from_start": round((ts_ms - start_ms) / 1000.0, 3),
            }
    return {"ts_unix_ms": 0, "services": [], "seconds_from_start": None}


def peak_increase(series: list[dict], initial: dict[str, int], service: str) -> int:
    peak = 0
    for point in series:
        replicas = int(point.get("replicas", {}).get(service, initial.get(service, 0)) or 0)
        peak = max(peak, replicas - int(initial.get(service, 0)))
    return peak


def total_peak_increase(series: list[dict], initial: dict[str, int]) -> int:
    peak = 0
    base = sum(int(v) for v in initial.values())
    for point in series:
        total = sum(int(point.get("replicas", {}).get(name, initial.get(name, 0)) or 0) for name in initial)
        peak = max(peak, total - base)
    return peak


def latest_trace_target(traces_rows: list[dict], start_ms: int) -> str:
    for row in reversed(traces_rows):
        ts_ms = int(row.get("ts_unix_ms", 0) or 0)
        if ts_ms < start_ms:
            continue
        payload = row.get("payload")
        items = payload if isinstance(payload, list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            target = str(item.get("identified_target") or item.get("target_service") or "").strip()
            if target:
                return target
    return ""


def summarize(case_dir: Path, arm: str) -> dict:
    case_cfg = load_json(case_dir / "case_config.json")
    request_summary = load_json(case_dir / "request_summary.json")
    collector_session = load_json(case_dir / "collector_session.json")
    replica_rows = load_ndjson(case_dir / "replica_counts.ndjson")
    traces_rows = load_ndjson(case_dir / "controller_traces.ndjson")

    expected = case_cfg.get("expected", {}) if isinstance(case_cfg, dict) else {}
    analysis = case_cfg.get("analysis", {}) if isinstance(case_cfg, dict) else {}
    initial = {
        str(name): int(replicas)
        for name, replicas in (case_cfg.get("initial_replicas") or {}).items()
    }
    if not initial:
        initial = {"front-end": 1, "catalogue": 1, "carts": 1, "orders": 1, "user": 1, "payment": 1, "shipping": 1}
    deployments = sorted(initial.keys())

    start_ms = int(request_summary.get("started_at_unix_ms") or collector_session.get("collection_started_at_unix_ms") or 0)
    series = extract_replica_series(replica_rows, deployments)
    first_action = first_scale_action(series, initial, start_ms)
    expected_service = str(expected.get("bottleneck_service", "") or "").strip()
    expected_reason = str(expected.get("reason_class", "") or "").strip()
    trigger_service = str(analysis.get("root_service", "") or "").strip()
    first_services = list(first_action.get("services") or [])
    first_action_service = ",".join(first_services) if first_services else ""
    no_scale = not first_services

    correct_scale_target = False
    correct_no_scale_behavior = False
    if expected_reason == "external_or_unmonitored_delay":
        correct_no_scale_behavior = no_scale
    elif expected_reason == "downstream_delay":
        correct_scale_target = bool(first_services) and expected_service in first_services and trigger_service not in first_services
    else:
        correct_scale_target = bool(first_services) and expected_service in first_services

    decision_quality = correct_no_scale_behavior or correct_scale_target
    summary = {
        "case_name": case_cfg.get("case_name", case_dir.name),
        "arm": arm,
        "scenario_type": expected_reason,
        "trigger_service": trigger_service,
        "expected_service": expected_service,
        "expected_reason_class": expected_reason,
        "first_action_time_seconds": first_action.get("seconds_from_start"),
        "first_scaled_service": first_action_service or None,
        "scaling_magnitude_expected_service": peak_increase(series, initial, expected_service) if expected_service else 0,
        "scaling_magnitude_total": total_peak_increase(series, initial),
        "correct_scale_target": correct_scale_target,
        "correct_no_scale_behavior": correct_no_scale_behavior,
        "decision_quality": decision_quality,
        "thrive_identified_target": latest_trace_target(traces_rows, start_ms) if arm == "thrivescale" else "",
        "client": {
            "success_rps": request_summary.get("success_rps"),
            "successful_responses": request_summary.get("successful_responses"),
            "failed_responses": request_summary.get("failed_responses"),
            "client_p90_latency_ms": request_summary.get("client_p90_latency_ms"),
        },
    }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize one autoscaler scenario case.")
    parser.add_argument("--case-dir", required=True, type=Path)
    parser.add_argument("--arm", required=True)
    args = parser.parse_args()

    summary = summarize(args.case_dir, args.arm)
    out_path = args.case_dir / "scenario_summary.json"
    out_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
