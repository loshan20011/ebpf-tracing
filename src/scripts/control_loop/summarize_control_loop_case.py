#!/usr/bin/env python3
import argparse
import json
from collections import Counter
from pathlib import Path
from statistics import median, quantiles


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


def percentile_p90(values: list[float]) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    return float(quantiles(values, n=10, method="inclusive")[8])


def bucket_requests(request_rows: list[dict], bucket_seconds: int = 10) -> list[dict]:
    if not request_rows:
        return []
    bucket_ms = max(1, int(bucket_seconds * 1000))
    start_ms = safe_int(request_rows[0].get("ts_unix_ms", 0), 0)
    buckets: dict[int, list[dict]] = {}
    for row in request_rows:
        ts_ms = safe_int(row.get("ts_unix_ms", 0), 0)
        bucket_start = start_ms + (((ts_ms - start_ms) // bucket_ms) * bucket_ms)
        buckets.setdefault(bucket_start, []).append(row)
    out = []
    for bucket_start in sorted(buckets.keys()):
        rows = buckets[bucket_start]
        success_latencies = [safe_float(row.get("latency_ms", 0.0), 0.0) for row in rows if row.get("success")]
        success_count = sum(1 for row in rows if row.get("success"))
        out.append(
            {
                "ts_unix_ms": bucket_start,
                "client_p90_latency_ms": round(percentile_p90(success_latencies), 3) if success_latencies else None,
                "successful_responses": success_count,
                "failed_responses": sum(1 for row in rows if not row.get("success")),
                "client_rps": round(success_count / max(bucket_seconds, 0.001), 3),
            }
        )
    return out


def filter_rows_by_window(rows: list[dict], start_ms: int, end_ms: int) -> list[dict]:
    out = []
    for row in rows:
        ts_ms = int(row.get("ts_unix_ms", 0) or 0)
        if start_ms <= ts_ms <= end_ms:
            out.append(row)
    return out


def payload_items_in_window(rows: list[dict], payload_key: str, start_ms: int, end_ms: int) -> list[dict]:
    items: list[dict] = []
    for row in rows:
        payload = row.get("payload")
        if isinstance(payload, dict):
            candidate = payload.get(payload_key, payload if payload_key == "" else None)
            if isinstance(candidate, list):
                for item in candidate:
                    if isinstance(item, dict):
                        ts_ms = int(item.get("ts_unix_ms", 0) or 0)
                        if start_ms <= ts_ms <= end_ms:
                            items.append(item)
            elif isinstance(candidate, dict):
                ts_ms = int(candidate.get("ts_unix_ms", 0) or 0)
                if start_ms <= ts_ms <= end_ms:
                    items.append(candidate)
        elif isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    ts_ms = int(item.get("ts_unix_ms", 0) or 0)
                    if start_ms <= ts_ms <= end_ms:
                        items.append(item)
    return items


def safe_float(value, default=0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def safe_int(value, default=0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def graph_metric(row: dict, service: str) -> dict:
    payload = row.get("payload") or {}
    metrics = payload.get("metrics", {}) if isinstance(payload, dict) else {}
    item = metrics.get(service, {}) if isinstance(metrics, dict) else {}
    return item if isinstance(item, dict) else {}


def metric_timeseries_sample(row: dict, service: str, slo_latency_ms: float) -> dict:
    ts_ms = safe_int(row.get("ts_unix_ms", 0), 0)
    metric = graph_metric(row, service)
    service_p90_ms = safe_float(metric.get("p90_latency", metric.get("platform_p90_ms", 0.0)), 0.0)
    truth_p90_ms = safe_float(metric.get("truth_p90_latency_ms", 0.0), 0.0)
    return {
        "ts_unix_ms": ts_ms,
        "service": service,
        "service_p90_latency_ms": round(service_p90_ms, 3) if service_p90_ms > 0 else None,
        "truth_p90_latency_ms": round(truth_p90_ms, 3) if truth_p90_ms > 0 else None,
        "service_handling_latency_ms": round(safe_float(metric.get("service_handling_latency", 0.0), 0.0), 3),
        "dependency_attributed_latency_ms": round(safe_float(metric.get("dependency_attributed_latency", 0.0), 0.0), 3),
        "external_wait_latency_ms": round(safe_float(metric.get("external_wait_latency", 0.0), 0.0), 3),
        "runq_p90_latency_ms": round(safe_float(metric.get("runq_p90_latency", 0.0), 0.0), 3),
        "cpu_throttle_ratio": round(safe_float(metric.get("cpu_throttle_ratio", 0.0), 0.0), 4),
        "rps": round(safe_float(metric.get("rps", 0.0), 0.0), 3),
        "slo_latency_ms": round(slo_latency_ms, 3),
        "above_slo": bool(slo_latency_ms > 0 and service_p90_ms > slo_latency_ms),
    }


def replica_map(row: dict) -> dict[str, int]:
    payload = row.get("payload") or {}
    items = payload.get("items", []) if isinstance(payload, dict) else []
    out: dict[str, int] = {}
    for item in items:
        name = str(item.get("metadata", {}).get("name", "")).strip()
        if not name:
            continue
        out[name] = safe_int(item.get("spec", {}).get("replicas", 0), 0)
    return out


def control_slo_map(rows: list[dict]) -> dict[str, float]:
    for row in reversed(rows):
        payload = row.get("payload") or {}
        slos = payload.get("slos", {}) if isinstance(payload, dict) else {}
        if not isinstance(slos, dict):
            continue
        out: dict[str, float] = {}
        for service, cfg in slos.items():
            if isinstance(cfg, dict):
                out[str(service)] = safe_float(cfg.get("sloLatency", 0.0), 0.0)
        if out:
            return out
    return {}


def infer_interval_seconds(rows: list[dict], fallback: float) -> float:
    if len(rows) < 2:
        return float(fallback)
    diffs = []
    prev = None
    for row in rows:
        ts_ms = safe_int(row.get("ts_unix_ms", 0), 0)
        if prev is not None and ts_ms > prev:
            diffs.append((ts_ms - prev) / 1000.0)
        prev = ts_ms
    return float(median(diffs)) if diffs else float(fallback)


def latest_replicas_at_or_before(replica_points: list[tuple[int, dict[str, int]]], ts_ms: int) -> dict[str, int]:
    latest: dict[str, int] = {}
    for point_ts, payload in replica_points:
        if point_ts <= ts_ms:
            latest = payload
        else:
            break
    return latest


def count_replica_changes(replica_points: list[tuple[int, dict[str, int]]], service: str) -> int:
    changes = 0
    prev = None
    for _, payload in replica_points:
        current = safe_int(payload.get(service, 0), 0)
        if prev is not None and current != prev:
            changes += 1
        prev = current
    return changes


def first_replica_change_ts(replica_points: list[tuple[int, dict[str, int]]], service: str) -> int | None:
    prev = None
    for ts_ms, payload in replica_points:
        current = safe_int(payload.get(service, 0), 0)
        if prev is not None and current != prev:
            return ts_ms
        prev = current
    return None


def count_oscillations(actions: list[dict]) -> int:
    directions = []
    for action in actions:
        decision = str(action.get("decision", "")).strip().lower()
        if decision == "scale_up":
            directions.append(1)
        elif decision == "scale_down":
            directions.append(-1)
    oscillations = 0
    prev = None
    for direction in directions:
        if prev is not None and direction != prev:
            oscillations += 1
        prev = direction
    return oscillations


def format_seconds(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    return f"{seconds:.1f}s"


def summarize(case_dir: Path, phase_name: str) -> dict:
    case_config = load_json(case_dir / "case_config.json")
    request_summary = load_json(case_dir / "request_summary.json")
    session_meta = load_json(case_dir / "collector_session.json")
    analysis = case_config.get("analysis", {})
    expected = case_config.get("expected", {})

    case_start_ms = int(
        request_summary.get("started_at_unix_ms")
        or session_meta.get("collection_started_at_unix_ms")
        or session_meta.get("started_at_unix_ms")
        or 0
    )
    case_end_ms = int(
        request_summary.get("ended_at_unix_ms")
        or session_meta.get("collection_ended_at_unix_ms")
        or 0
    )

    request_rows = filter_rows_by_window(load_ndjson(case_dir / "request_log.ndjson"), case_start_ms, case_end_ms)
    graph_rows = filter_rows_by_window(load_ndjson(case_dir / "aggregator_graph.ndjson"), case_start_ms, case_end_ms)
    trace_rows = filter_rows_by_window(load_ndjson(case_dir / "controller_traces.ndjson"), case_start_ms, case_end_ms)
    control_state_rows = filter_rows_by_window(load_ndjson(case_dir / "control_state.ndjson"), case_start_ms, case_end_ms)
    replica_rows = filter_rows_by_window(load_ndjson(case_dir / "replica_counts.ndjson"), case_start_ms, case_end_ms)

    root_service = str(analysis.get("root_service", "gateway"))
    slo_service = str(analysis.get("slo_service", root_service))
    expected_target_service = str(expected.get("target_service") or analysis.get("expected_target_service") or "")
    initial_replicas_cfg = {
        str(name): safe_int(value, 1) for name, value in (case_config.get("initial_replicas") or {}).items()
    }

    slo_map = control_slo_map(control_state_rows)
    if not slo_map and graph_rows:
        latest_metric = graph_metric(graph_rows[-1], slo_service)
        slo_value = safe_float(latest_metric.get("slo_latency", 0.0), 0.0)
        if slo_value > 0:
            slo_map[slo_service] = slo_value
    slo_latency_ms = safe_float(slo_map.get(slo_service, 0.0), 0.0)

    interval_seconds = infer_interval_seconds(graph_rows, safe_float(case_config.get("interval_seconds", 5), 5.0))
    trace_events = payload_items_in_window(trace_rows, "traces", case_start_ms, case_end_ms)
    actions = [row for row in trace_events if str(row.get("decision", "")).lower() in {"scale_up", "scale_down"}]
    scale_up_actions = [row for row in actions if str(row.get("decision", "")).lower() == "scale_up"]
    scale_down_actions = [row for row in actions if str(row.get("decision", "")).lower() == "scale_down"]
    under_pressure_traces = [row for row in trace_events if bool(row.get("under_pressure", False))]
    blocked_reason_counter = Counter(
        str(row.get("blocked_by", "")).strip()
        for row in trace_events
        if str(row.get("decision", "")).lower() == "no_scale" and str(row.get("blocked_by", "")).strip()
    )

    action_targets = [str(row.get("service") or row.get("node") or row.get("target_service") or "").strip() for row in actions]
    pressure_targets = [str(row.get("service") or row.get("node") or "").strip() for row in under_pressure_traces]
    target_counter = Counter([name for name in action_targets if name]) or Counter([name for name in pressure_targets if name])
    dominant_target_service = target_counter.most_common(1)[0][0] if target_counter else root_service

    replica_points = [(safe_int(row.get("ts_unix_ms", 0), 0), replica_map(row)) for row in replica_rows]
    replica_series = []
    violation_window_count = 0
    recovery_ts_ms = None
    first_breach_ts_ms = None
    stable_below_count = 0
    peak_replicas_any = 0
    peak_replicas_target = 0
    under_provisioned_duration_seconds = 0.0
    over_provisioned_duration_seconds = 0.0

    for row in graph_rows:
        ts_ms = safe_int(row.get("ts_unix_ms", 0), 0)
        metric = graph_metric(row, slo_service)
        p90_ms = safe_float(metric.get("p90_latency", metric.get("platform_p90_ms", 0.0)), 0.0)
        rps = safe_float(metric.get("rps", 0.0), 0.0)
        above_slo = bool(slo_latency_ms > 0 and p90_ms > slo_latency_ms)
        if above_slo:
            violation_window_count += 1
            stable_below_count = 0
            if first_breach_ts_ms is None:
                first_breach_ts_ms = ts_ms
        elif first_breach_ts_ms is not None:
            stable_below_count += 1
            if stable_below_count >= 2 and recovery_ts_ms is None:
                recovery_ts_ms = ts_ms

        replicas = latest_replicas_at_or_before(replica_points, ts_ms)
        current_peak_any = max(replicas.values()) if replicas else 0
        peak_replicas_any = max(peak_replicas_any, current_peak_any)
        dominant_replicas = safe_int(replicas.get(dominant_target_service, initial_replicas_cfg.get(dominant_target_service, 0)), 0)
        peak_replicas_target = max(peak_replicas_target, dominant_replicas)
        initial_target_replicas = initial_replicas_cfg.get(dominant_target_service, 1)

        if above_slo and dominant_replicas < max(initial_target_replicas, peak_replicas_target):
            under_provisioned_duration_seconds += interval_seconds
        if not above_slo and dominant_replicas > initial_target_replicas:
            over_provisioned_duration_seconds += interval_seconds

        replica_series.append(
            {
                "ts_unix_ms": ts_ms,
                "service": slo_service,
                "p90_latency_ms": round(p90_ms, 3),
                "slo_latency_ms": round(slo_latency_ms, 3),
                "above_slo": above_slo,
                "rps": round(rps, 3),
                "dominant_target_service": dominant_target_service,
                "dominant_target_replicas": dominant_replicas,
                "replicas": replicas,
            }
        )

    request_slo_violations = 0
    success_latencies = []
    for row in request_rows:
        if row.get("success"):
            latency_ms = safe_float(row.get("latency_ms", 0.0), 0.0)
            success_latencies.append(latency_ms)
            if slo_latency_ms > 0 and latency_ms > slo_latency_ms:
                request_slo_violations += 1
    client_timeseries = bucket_requests(request_rows, bucket_seconds=max(1, int(round(interval_seconds))))
    client_violation_window_count = sum(
        1 for row in client_timeseries if safe_float(row.get("client_p90_latency_ms", 0.0), 0.0) > slo_latency_ms
    )

    first_action_ts_ms = safe_int(actions[0].get("ts_unix_ms", 0), 0) if actions else None
    final_replica_map = replica_points[-1][1] if replica_points else initial_replicas_cfg
    final_replica_target = safe_int(final_replica_map.get(dominant_target_service, initial_replicas_cfg.get(dominant_target_service, 1)), 1)
    actual_replica_change_count = count_replica_changes(replica_points, dominant_target_service)
    first_actual_replica_change_ts = first_replica_change_ts(replica_points, dominant_target_service)
    time_to_first_action_seconds = max(0.0, (first_action_ts_ms - case_start_ms) / 1000.0) if first_action_ts_ms else None
    time_to_first_actual_replica_change_seconds = (
        max(0.0, (first_actual_replica_change_ts - case_start_ms) / 1000.0)
        if first_actual_replica_change_ts
        else None
    )
    rollout_delay_after_first_action_seconds = (
        max(0.0, (first_actual_replica_change_ts - first_action_ts_ms) / 1000.0)
        if first_action_ts_ms and first_actual_replica_change_ts and first_actual_replica_change_ts >= first_action_ts_ms
        else None
    )
    time_to_recovery_seconds = max(0.0, (recovery_ts_ms - case_start_ms) / 1000.0) if recovery_ts_ms else None
    effective_violation_window_count = max(violation_window_count, client_violation_window_count)
    total_time_above_slo_seconds = round(effective_violation_window_count * interval_seconds, 3)
    oscillation_count = count_oscillations(actions)
    targeted_root_count = sum(1 for action in actions if str(action.get("service") or action.get("node") or "") == root_service)
    expected_target_ratio = (
        sum(1 for action in actions if str(action.get("service") or action.get("node") or "") == expected_target_service) / len(actions)
        if actions and expected_target_service
        else None
    )
    final_stable = len({row["dominant_target_replicas"] for row in replica_series[-4:]}) <= 1 if replica_series else True
    protective_root_count = sum(1 for row in trace_events if bool(row.get("protective_root_used", False)))
    configured_max_replicas = safe_int(
        (session_meta.get("slo_setup", {}) or {}).get("replica_bounds", {}).get("maxReplicas", 0),
        0,
    )
    if not configured_max_replicas:
        configured_max_replicas = safe_int(
            (session_meta.get("slo_setup", {}) or {}).get("gateway_confirmed", {}).get("maxReplicas", 0),
            0,
        )
    if not configured_max_replicas:
        configured_max_replicas = safe_int(case_config.get("replica_bounds", {}).get("maxReplicas", 0), 0)
    capacity_ceiling_reached = bool(configured_max_replicas and peak_replicas_target >= configured_max_replicas)
    recovered_below_slo = recovery_ts_ms is not None
    recovered_before_capacity_ceiling = bool(recovered_below_slo and not capacity_ceiling_reached)
    delayed_downscale_respected = None
    if scale_down_actions:
        first_downscale_ts = safe_int(scale_down_actions[0].get("ts_unix_ms", 0), 0)
        delayed_downscale_respected = bool(recovery_ts_ms is not None and first_downscale_ts >= recovery_ts_ms)
    if capacity_ceiling_reached and not recovered_below_slo:
        capacity_limit_note = "configured replica ceiling was reached before SLO recovery"
    elif actual_replica_change_count > 0 and not recovered_below_slo:
        capacity_limit_note = "replicas increased but SLO still did not recover; remaining node capacity or per-pod capacity may be limiting"
    elif rollout_delay_after_first_action_seconds is not None and rollout_delay_after_first_action_seconds > interval_seconds:
        capacity_limit_note = "rollout delay slowed the arrival of added capacity"
    else:
        capacity_limit_note = ""
    service_timeseries = [metric_timeseries_sample(row, slo_service, slo_latency_ms) for row in graph_rows]

    actual_pattern_behavior = (
        f"{len(scale_up_actions)} scale-up proposals and {len(scale_down_actions)} scale-down proposals; "
        f"{actual_replica_change_count} actual replica changes; "
        f"dominant target {dominant_target_service}; first action {format_seconds(time_to_first_action_seconds)}; "
        f"oscillation {oscillation_count}; protective root uses {protective_root_count}"
    )
    final_replica_behavior = (
        f"{dominant_target_service} peaked at {peak_replicas_target} and ended at {final_replica_target}; "
        f"cluster peak replica count {peak_replicas_any}; final window stable={str(final_stable).lower()}"
    )
    slo_protection_result = (
        f"{slo_service} SLO {slo_latency_ms:.1f}ms; above SLO for {total_time_above_slo_seconds:.1f}s; "
        f"{request_slo_violations} successful requests above SLO; recovery {format_seconds(time_to_recovery_seconds)}"
    )

    pattern_pass = True
    replica_pass = True
    slo_pass = True

    if expected.get("require_scale_up") and len(scale_up_actions) < 1:
        pattern_pass = False
    if expected.get("require_scale_down") and len(scale_down_actions) < 1:
        replica_pass = False
    if "max_scale_action_count" in expected:
        pattern_pass = pattern_pass and len(actions) <= safe_int(expected.get("max_scale_action_count"), 0)
    if "max_oscillation_count" in expected:
        pattern_pass = pattern_pass and oscillation_count <= safe_int(expected.get("max_oscillation_count"), 0)
    if "max_peak_replicas" in expected:
        replica_pass = replica_pass and peak_replicas_target <= safe_int(expected.get("max_peak_replicas"), 0)
    if "min_peak_replicas" in expected:
        replica_pass = replica_pass and peak_replicas_target >= safe_int(expected.get("min_peak_replicas"), 0)
    if "max_time_above_slo_seconds" in expected:
        slo_pass = slo_pass and total_time_above_slo_seconds <= safe_float(expected.get("max_time_above_slo_seconds"), 0.0)
    if "max_time_to_first_action_seconds" in expected and time_to_first_action_seconds is not None:
        pattern_pass = pattern_pass and time_to_first_action_seconds <= safe_float(expected.get("max_time_to_first_action_seconds"), 0.0)
    if expected.get("require_stable_final_replica"):
        replica_pass = replica_pass and final_stable
    if expected.get("require_delayed_downscale"):
        replica_pass = replica_pass and bool(scale_down_actions)
        if scale_down_actions and recovery_ts_ms is not None:
            first_downscale_ts = safe_int(scale_down_actions[0].get("ts_unix_ms", 0), 0)
            replica_pass = replica_pass and first_downscale_ts >= recovery_ts_ms
    if expected.get("require_recovery_below_slo"):
        slo_pass = slo_pass and recovered_below_slo
    if expected_target_service:
        pattern_pass = pattern_pass and dominant_target_service == expected_target_service
    if "max_root_target_count" in expected:
        pattern_pass = pattern_pass and targeted_root_count <= safe_int(expected.get("max_root_target_count"), 0)
    if "min_expected_target_ratio" in expected and expected_target_ratio is not None:
        pattern_pass = pattern_pass and expected_target_ratio >= safe_float(expected.get("min_expected_target_ratio"), 0.0)

    summary = {
        "case_name": str(case_config.get("case_name", case_dir.name)),
        "phase_name": phase_name,
        "mode": str(case_config.get("mode", "")),
        "expected": {
            "traffic_pattern": str(expected.get("traffic_pattern", "")),
            "controller_behavior": str(expected.get("controller_behavior", "")),
            "replica_trend": str(expected.get("replica_trend", "")),
            "slo_behavior": str(expected.get("slo_behavior", "")),
            "pass_fail_criteria": str(expected.get("pass_fail_criteria", "")),
            "pattern_behavior": str(expected.get("pattern_behavior", "")),
            "final_replica_behavior": str(expected.get("final_replica_behavior", "")),
            "slo_protection_result": str(expected.get("slo_protection_result", "")),
            "target_service": expected_target_service,
        },
        "client": {
            "p90_latency_ms": round(percentile_p90(success_latencies), 3),
            "success_rps": round(
                safe_int(request_summary.get("successful_responses", 0), 0)
                / max(safe_float(request_summary.get("duration_seconds", 0.0), 0.001), 0.001),
                3,
            ),
            "successful_responses": safe_int(request_summary.get("successful_responses", 0), 0),
            "failed_responses": safe_int(request_summary.get("failed_responses", 0), 0),
        },
        "control_loop": {
            "slo_service": slo_service,
            "slo_latency_ms": round(slo_latency_ms, 3),
            "dominant_target_service": dominant_target_service,
            "actual_pattern_behavior": actual_pattern_behavior,
            "final_replica_behavior": final_replica_behavior,
            "slo_protection_result": slo_protection_result,
            "time_to_first_action_seconds": round(time_to_first_action_seconds, 3) if time_to_first_action_seconds is not None else None,
            "time_to_first_actual_replica_change_seconds": round(time_to_first_actual_replica_change_seconds, 3) if time_to_first_actual_replica_change_seconds is not None else None,
            "rollout_delay_after_first_action_seconds": round(rollout_delay_after_first_action_seconds, 3) if rollout_delay_after_first_action_seconds is not None else None,
            "time_to_recovery_below_slo_seconds": round(time_to_recovery_seconds, 3) if time_to_recovery_seconds is not None else None,
            "slo_violation_count": request_slo_violations,
            "graph_violation_window_count": violation_window_count,
            "client_violation_window_count": client_violation_window_count,
            "violation_window_count": violation_window_count,
            "total_time_above_slo_seconds": total_time_above_slo_seconds,
            "scale_action_count": len(actions),
            "scale_up_count": len(scale_up_actions),
            "scale_down_count": len(scale_down_actions),
            "oscillation_count": oscillation_count,
            "peak_replicas": peak_replicas_target,
            "peak_replicas_any_service": peak_replicas_any,
            "under_provisioned_duration_seconds": round(under_provisioned_duration_seconds, 3),
            "over_provisioned_duration_seconds": round(over_provisioned_duration_seconds, 3),
            "provisioning_note": (
                f"under-provisioned for {under_provisioned_duration_seconds:.1f}s; "
                f"over-provisioned for {over_provisioned_duration_seconds:.1f}s"
            ),
            "final_target_replicas": final_replica_target,
            "actual_replica_change_count": actual_replica_change_count,
            "targeted_root_count": targeted_root_count,
            "protective_root_count": protective_root_count,
            "expected_target_ratio": round(expected_target_ratio, 3) if expected_target_ratio is not None else None,
            "configured_max_replicas": configured_max_replicas,
            "capacity_ceiling_reached": capacity_ceiling_reached,
            "recovered_below_slo": recovered_below_slo,
            "recovered_before_capacity_ceiling": recovered_before_capacity_ceiling,
            "delayed_downscale_respected": delayed_downscale_respected,
            "capacity_limit_note": capacity_limit_note,
            "blocked_by_counts": dict(blocked_reason_counter),
        },
        "pass_fail": {
            "pattern_pass": bool(pattern_pass),
            "replica_pass": bool(replica_pass),
            "slo_pass": bool(slo_pass),
            "overall_pass": bool(pattern_pass and replica_pass and slo_pass),
        },
    }

    timeseries = {
        "case_name": str(case_config.get("case_name", case_dir.name)),
        "root_service": root_service,
        "slo_service": slo_service,
        "dominant_target_service": dominant_target_service,
        "client_samples": client_timeseries,
        "service_samples": service_timeseries,
        "replica_samples": replica_series,
        "actions": actions,
    }
    (case_dir / "timeseries.json").write_text(json.dumps(timeseries, indent=2, sort_keys=True), encoding="utf-8")

    summary_md = "\n".join(
        [
            f"# {summary['case_name']}",
            "",
            "## Expected",
            f"- Traffic pattern: {summary['expected']['traffic_pattern']}",
            f"- Controller behavior: {summary['expected']['controller_behavior']}",
            f"- Replica trend: {summary['expected']['replica_trend']}",
            f"- SLO behavior: {summary['expected']['slo_behavior']}",
            f"- Pass/fail criteria: {summary['expected']['pass_fail_criteria']}",
            f"- Pattern: {summary['expected']['pattern_behavior']}",
            f"- Final replica behavior: {summary['expected']['final_replica_behavior']}",
            f"- SLO protection: {summary['expected']['slo_protection_result']}",
            "",
            "## Actual",
            f"- Pattern: {summary['control_loop']['actual_pattern_behavior']}",
            f"- Final replica behavior: {summary['control_loop']['final_replica_behavior']}",
            f"- SLO protection: {summary['control_loop']['slo_protection_result']}",
            "",
            "## Key Metrics",
            f"- Time to first action: {format_seconds(summary['control_loop']['time_to_first_action_seconds'])}",
            f"- Time to first actual replica change: {format_seconds(summary['control_loop']['time_to_first_actual_replica_change_seconds'])}",
            f"- Rollout delay after first action: {format_seconds(summary['control_loop']['rollout_delay_after_first_action_seconds'])}",
            f"- Time to recovery below SLO: {format_seconds(summary['control_loop']['time_to_recovery_below_slo_seconds'])}",
            f"- Scale proposals: {summary['control_loop']['scale_action_count']}",
            f"- Actual replica changes: {summary['control_loop']['actual_replica_change_count']}",
            f"- Oscillation count: {summary['control_loop']['oscillation_count']}",
            f"- Peak replicas: {summary['control_loop']['peak_replicas']}",
            f"- Capacity ceiling reached: {summary['control_loop']['capacity_ceiling_reached']}",
            f"- Recovered below SLO: {summary['control_loop']['recovered_below_slo']}",
            f"- Capacity note: {summary['control_loop']['capacity_limit_note'] or 'n/a'}",
            f"- Blocked-by counts: {summary['control_loop']['blocked_by_counts']}",
            f"- Provisioning note: {summary['control_loop']['provisioning_note']}",
            "",
            "## Pass",
            f"- Pattern pass: {summary['pass_fail']['pattern_pass']}",
            f"- Replica pass: {summary['pass_fail']['replica_pass']}",
            f"- SLO pass: {summary['pass_fail']['slo_pass']}",
            f"- Overall pass: {summary['pass_fail']['overall_pass']}",
            "",
        ]
    )
    (case_dir / "summary.md").write_text(summary_md, encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize one control-loop evaluation case.")
    parser.add_argument("--case-dir", required=True, type=Path, help="Case result directory")
    parser.add_argument("--phase-name", default="control_loop", help="Logical phase name")
    args = parser.parse_args()

    summary = summarize(args.case_dir, args.phase_name)
    out_path = args.case_dir / "summary.json"
    out_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
