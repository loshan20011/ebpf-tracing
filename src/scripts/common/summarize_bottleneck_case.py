#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path
from statistics import median, quantiles


RUNQ_FIXED_THRESHOLD_MS = 3.0
RUNQ_BORDERLINE_MS = 2.5
LEAF_CPU_CONFIRM_RUNQ_MS = 2.7
CPU_THROTTLE_RATIO_THRESHOLD = 0.10
DEPENDENCY_DOMINANCE_RATIO = 1.25
EXTERNAL_DOMINANCE_RATIO = 1.25
ACTIVE_RPS_THRESHOLD = 0.5
CHILD_SIMILARITY_FLOOR = 0.70
CHILD_SIMILARITY_CEILING = 1.30
LOCAL_FRACTION_MIN = 0.40
DEPENDENCY_FRACTION_MAX = 0.50
EVALUATION_WINDOW_SECONDS = 40
MIN_CONSECUTIVE_MATCH_LOOPS = 2
TOP_LEVEL_REASON_CLASSES = {
    "local_cpu_pressure",
    "downstream_delay",
    "external_or_unmonitored_delay",
}


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


def p90(values: list[float]) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    return float(quantiles(values, n=10, method="inclusive")[8])


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


def metric_obj(metrics: dict, service: str) -> dict:
    item = metrics.get(service, {})
    return item if isinstance(item, dict) else {}


def filter_rows_by_window(rows: list[dict], start_ms: int, end_ms: int) -> list[dict]:
    out = []
    for row in rows:
        ts_ms = int(row.get("ts_unix_ms", 0) or 0)
        if start_ms <= ts_ms <= end_ms:
            out.append(row)
    return out


def topology_edges(graph_rows: list[dict]) -> list[str]:
    edges = set()
    for row in graph_rows:
        payload = row.get("payload") or {}
        topology = payload.get("topology", {}) if isinstance(payload, dict) else {}
        if not isinstance(topology, dict):
            continue
        for parent, children in topology.items():
            if isinstance(children, list):
                for child in children:
                    edges.add(f"{parent}->{child}")
    return sorted(edges)


def expected_edges_present(row: dict, expected_edges: list[str]) -> bool:
    payload = row.get("payload") or {}
    topology = payload.get("topology", {}) if isinstance(payload, dict) else {}
    if not isinstance(topology, dict):
        return False
    observed = set()
    for parent, children in topology.items():
        if isinstance(children, list):
            for child in children:
                observed.add(f"{parent}->{child}")
    return all(edge in observed for edge in expected_edges)


def latest_graph_payload(graph_rows: list[dict]) -> dict:
    if not graph_rows:
        return {}
    payload = graph_rows[-1].get("payload")
    return payload if isinstance(payload, dict) else {}


def metric_rows_by_service(graph_rows: list[dict], service: str) -> list[dict]:
    rows = []
    for row in graph_rows:
        payload = row.get("payload") or {}
        metrics = payload.get("metrics", {}) if isinstance(payload, dict) else {}
        metric = metrics.get(service, {}) if isinstance(metrics, dict) else {}
        if isinstance(metric, dict):
            rows.append(metric)
    return rows


def median_metric_value(graph_rows: list[dict], service: str, getter) -> float:
    values = []
    for metric in metric_rows_by_service(graph_rows, service):
        value = getter(metric)
        if value > 0:
            values.append(value)
    if not values:
        return 0.0
    return float(median(values))


def active_majority(graph_rows: list[dict], service: str) -> bool:
    rows = metric_rows_by_service(graph_rows, service)
    if not rows:
        return False
    positives = sum(1 for metric in rows if active_demand(metric))
    return positives >= max(1, len(rows) // 2)


def representative_metrics(graph_rows: list[dict], services: set[str]) -> dict:
    out = {}
    for service in services:
        rows = metric_rows_by_service(graph_rows, service)
        latest = rows[-1] if rows else {}
        candidate_rows = [
            row for row in rows
            if preferred_platform_p90(row) > 0
            or service_handling(row) > 0
            or dependency_latency(row) > 0
            or external_wait(row) > 0
        ]
        if candidate_rows:
            target_p90 = median(preferred_platform_p90(row) for row in candidate_rows)
            representative_row = min(
                candidate_rows,
                key=lambda row: (
                    abs(preferred_platform_p90(row) - target_p90),
                    -external_wait(row),
                    -dependency_latency(row),
                ),
            )
        else:
            representative_row = latest
        external_evidence_rows = [
            row for row in candidate_rows
            if external_wait(row) >= max(service_handling(row), 1.0)
        ]
        sustained_external_evidence = len(external_evidence_rows) >= 1 if candidate_rows else False
        out[service] = {
            "active_short": bool(representative_row.get("active_short", False)) if representative_row else active_majority(graph_rows, service),
            "platform_p90_ms": preferred_platform_p90(representative_row),
            "rps": preferred_rps(representative_row),
            "service_handling_latency": service_handling(representative_row),
            "dependency_attributed_latency": dependency_latency(representative_row),
            "external_wait_latency": external_wait(representative_row),
            "runq_p90_latency": runq_p90(representative_row),
            "cpu_throttle_ratio": cpu_throttle_ratio(representative_row),
            "sustained_external_evidence": sustained_external_evidence,
        }
    return out


def snapshot_metrics(row: dict, services: set[str]) -> dict:
    payload = row.get("payload") or {}
    metrics = payload.get("metrics", {}) if isinstance(payload, dict) else {}
    out = {}
    for service in services:
        metric = metrics.get(service, {}) if isinstance(metrics, dict) else {}
        metric = metric if isinstance(metric, dict) else {}
        out[service] = {
            "active_short": bool(metric.get("active_short", False)),
            "platform_p90_ms": preferred_platform_p90(metric),
            "rps": preferred_rps(metric),
            "service_handling_latency": service_handling(metric),
            "dependency_attributed_latency": dependency_latency(metric),
            "external_wait_latency": external_wait(metric),
            "runq_p90_latency": runq_p90(metric),
            "cpu_throttle_ratio": cpu_throttle_ratio(metric),
            "sustained_external_evidence": external_wait(metric) >= max(service_handling(metric), 1.0),
        }
    return out


def preferred_rps(metric: dict) -> float:
    return safe_float(metric.get("rps", 0.0), 0.0)


def service_handling(metric: dict) -> float:
    return safe_float(metric.get("service_handling_latency", metric.get("exclusive_delay", 0.0)), 0.0)


def dependency_latency(metric: dict) -> float:
    return safe_float(metric.get("dependency_attributed_latency", 0.0), 0.0)


def external_wait(metric: dict) -> float:
    return safe_float(metric.get("external_wait_latency", 0.0), 0.0)


def runq_p90(metric: dict) -> float:
    return safe_float(metric.get("runq_p90_latency", 0.0), 0.0)


def cpu_throttle_ratio(metric: dict) -> float:
    return safe_float(metric.get("cpu_throttle_ratio", 0.0), 0.0)


def preferred_platform_p90(metric: dict) -> float:
    return safe_float(
        metric.get("p90_latency", metric.get("platform_p90_ms", metric.get("latency", 0.0))),
        0.0,
    )


def total_latency(metric: dict) -> float:
    return max(
        preferred_platform_p90(metric),
        service_handling(metric) + dependency_latency(metric) + external_wait(metric),
        0.0,
    )


def local_fraction(metric: dict) -> float:
    return service_handling(metric) / max(total_latency(metric), 0.001)


def dependency_fraction(metric: dict) -> float:
    return dependency_latency(metric) / max(total_latency(metric), 0.001)


def active_demand(metric: dict) -> bool:
    return bool(metric.get("active_short", False)) and preferred_rps(metric) >= ACTIVE_RPS_THRESHOLD


def normalize_reason_class(value: str) -> str:
    reason = str(value or "")
    if reason in {"local_cpu_pressure", "local_unclear_or_non_cpu", "local_bottleneck"}:
        return "local_cpu_pressure"
    if reason == "external_delay":
        return "external_or_unmonitored_delay"
    return reason


def sustained_external_evidence(metric: dict) -> bool:
    return bool(metric.get("sustained_external_evidence", False))


def dependency_dominant(metric: dict) -> bool:
    local_ms = service_handling(metric)
    dep_ms = dependency_latency(metric)
    return dep_ms > 0 and dep_ms >= max(local_ms * DEPENDENCY_DOMINANCE_RATIO, local_ms + 1.0)


def external_dominant(metric: dict) -> bool:
    local_ms = service_handling(metric)
    dep_ms = dependency_latency(metric)
    ext_ms = external_wait(metric)
    return ext_ms > 0 and ext_ms >= max(local_ms * EXTERNAL_DOMINANCE_RATIO, dep_ms * EXTERNAL_DOMINANCE_RATIO)


def local_handling_dominant(metric: dict) -> bool:
    local_ms = service_handling(metric)
    dep_ms = dependency_latency(metric)
    ext_ms = external_wait(metric)
    return local_ms >= max(dep_ms * 1.1, ext_ms * 1.1, 1.0)


def dependency_or_unexplained_delay_high(metric: dict) -> bool:
    dep_ms = dependency_latency(metric)
    ext_ms = external_wait(metric)
    local_ms = service_handling(metric)
    return max(dep_ms, ext_ms) >= max(local_ms * 1.1, 1.0)


def local_cpu_pressure(metric: dict) -> bool:
    # In evaluation, treat throttle evidence as a strong corroborator for leaf CPU pressure.
    # This prevents obviously IO-shaped leaves from being labeled CPU-bound just because
    # host-side run queue delay is elevated.
    if cpu_throttle_ratio(metric) <= 0.0 and service_handling(metric) >= 60.0:
        return False
    return (
        active_demand(metric)
        and runq_p90(metric) >= LEAF_CPU_CONFIRM_RUNQ_MS
        and local_fraction(metric) >= LOCAL_FRACTION_MIN
        and dependency_fraction(metric) < DEPENDENCY_FRACTION_MAX
        and local_handling_dominant(metric)
    )


def borderline_runq_cpu_evidence(metric: dict) -> bool:
    return RUNQ_BORDERLINE_MS <= runq_p90(metric) < LEAF_CPU_CONFIRM_RUNQ_MS


def local_unclear_non_cpu(metric: dict) -> bool:
    local_ms = service_handling(metric)
    dep_ms = dependency_latency(metric)
    ext_ms = external_wait(metric)
    return (
        active_demand(metric)
        and local_ms >= max(dep_ms * 1.1, ext_ms * 1.1, 1.0)
        and runq_p90(metric) < LEAF_CPU_CONFIRM_RUNQ_MS
    )


def child_match_score(metrics: dict, current: str, child: str) -> float:
    current_metric = metric_obj(metrics, current)
    child_metric = metric_obj(metrics, child)
    expected_child_ms = max(dependency_latency(current_metric), 0.0)
    child_runq = runq_p90(child_metric)
    score = 0.0
    if expected_child_ms > 0:
        child_p90 = preferred_platform_p90(child_metric)
        child_local = service_handling(child_metric)
        low = max(expected_child_ms * CHILD_SIMILARITY_FLOOR, 1.0)
        high = max(expected_child_ms * CHILD_SIMILARITY_CEILING, low)
        if low <= child_p90 <= high:
            score += 3.0
        if low <= child_local <= high:
            score += 3.0
        if child_p90 >= max(expected_child_ms * 0.7, 1.0):
            score += 1.5
        if child_local >= max(expected_child_ms * 0.7, 1.0):
            score += 1.5
    if expected_child_ms > 0 and service_handling(child_metric) >= max(expected_child_ms * 0.7, 1.0):
        score += 3.0
    if expected_child_ms > 0 and child_runq >= RUNQ_FIXED_THRESHOLD_MS:
        score += 0.75
    if dependency_latency(current_metric) >= max(total_latency(current_metric) * DEPENDENCY_FRACTION_MAX, 1.0):
        score += 0.5
    return score


def pick_strongest_child(metrics: dict, topology: dict, current: str, monitored: set[str]) -> str | None:
    children = [child for child in topology.get(current, []) if child in monitored]
    if not children:
        return None

    scored = sorted(
        ((child_match_score(metrics, current, child), child) for child in children),
        key=lambda row: (-row[0], row[1]),
    )
    if scored and scored[0][0] >= 3.0:
        return scored[0][1]
    return None


def detect_bottleneck(metrics: dict, topology: dict, root: str, monitored: set[str]) -> dict:
    current = root
    path = [current]
    seen = {current}
    path_reason = None

    while True:
        metric = metric_obj(metrics, current)
        child = pick_strongest_child(metrics, topology, current, monitored)
        if child and (dependency_dominant(metric) or external_dominant(metric) or dependency_or_unexplained_delay_high(metric)):
            path_reason = "downstream_delay"
            current = child
            path.append(child)
            seen.add(child)
            continue

        if sustained_external_evidence(metric):
            leaf_reason = "external_or_unmonitored_delay"
            return {
                "bottleneck_service": current,
                "reason_class": path_reason or leaf_reason,
                "path_reason_class": path_reason,
                "leaf_reason_class": leaf_reason,
                "path": path,
                "reason_detail": leaf_reason,
            }

        if local_cpu_pressure(metric):
            leaf_reason = "local_cpu_pressure"
            return {
                "bottleneck_service": current,
                "reason_class": path_reason or leaf_reason,
                "path_reason_class": path_reason,
                "leaf_reason_class": leaf_reason,
                "path": path,
                "reason_detail": leaf_reason,
            }

        if (
            local_fraction(metric) >= LOCAL_FRACTION_MIN
            and dependency_fraction(metric) < DEPENDENCY_FRACTION_MAX
            and local_handling_dominant(metric)
        ):
            leaf_reason = "local_unclear_or_non_cpu"
            return {
                "bottleneck_service": current,
                "reason_class": path_reason or leaf_reason,
                "path_reason_class": path_reason,
                "leaf_reason_class": leaf_reason,
                "path": path,
                "reason_detail": "local_not_cpu_scaleable",
            }

        if local_unclear_non_cpu(metric):
            leaf_reason = "local_unclear_or_non_cpu"
            return {
                "bottleneck_service": current,
                "reason_class": path_reason or leaf_reason,
                "path_reason_class": path_reason,
                "leaf_reason_class": leaf_reason,
                "path": path,
                "reason_detail": leaf_reason,
            }

        if dependency_or_unexplained_delay_high(metric):
            leaf_reason = "external_or_unmonitored_delay"
            return {
                "bottleneck_service": current,
                "reason_class": path_reason or leaf_reason,
                "path_reason_class": path_reason,
                "leaf_reason_class": leaf_reason,
                "path": path,
                "reason_detail": leaf_reason,
            }

        leaf_reason = "local_unclear_or_non_cpu"
        return {
            "bottleneck_service": current,
            "reason_class": path_reason or leaf_reason,
            "path_reason_class": path_reason,
            "leaf_reason_class": leaf_reason,
            "path": path,
            "reason_detail": "fallback_unclear",
        }


def top_level_reason_class(detection: dict) -> str:
    path_reason = normalize_reason_class(str(detection.get("path_reason_class") or ""))
    leaf_reason = normalize_reason_class(str(detection.get("leaf_reason_class") or ""))
    if leaf_reason == "external_or_unmonitored_delay":
        return "external_or_unmonitored_delay"
    if path_reason == "downstream_delay":
        return "downstream_delay"
    if leaf_reason == "local_cpu_pressure":
        return "local_cpu_pressure"
    return leaf_reason or normalize_reason_class(str(detection.get("reason_class") or ""))


def summarize(case_dir: Path, phase_name: str) -> dict:
    case_config = load_json(case_dir / "case_config.json")
    request_summary = load_json(case_dir / "request_summary.json")
    collector_session = load_json(case_dir / "collector_session.json")

    start_ms = int(
        request_summary.get("started_at_unix_ms")
        or collector_session.get("collection_started_at_unix_ms")
        or 0
    )
    end_ms = int(
        request_summary.get("ended_at_unix_ms")
        or collector_session.get("collection_ended_at_unix_ms")
        or 0
    )

    request_rows = filter_rows_by_window(load_ndjson(case_dir / "request_log.ndjson"), start_ms, end_ms)
    graph_rows = filter_rows_by_window(load_ndjson(case_dir / "aggregator_graph.ndjson"), start_ms, end_ms)
    evaluation_start_ms = max(start_ms, end_ms - (EVALUATION_WINDOW_SECONDS * 1000))
    eval_graph_rows = filter_rows_by_window(graph_rows, evaluation_start_ms, end_ms)
    if not eval_graph_rows:
        eval_graph_rows = graph_rows
    payload = latest_graph_payload(graph_rows)
    metrics = payload.get("metrics", {}) if isinstance(payload, dict) else {}
    topology = payload.get("topology", {}) if isinstance(payload, dict) else {}

    success_latencies = [safe_float(row.get("latency_ms", 0.0), 0.0) for row in request_rows if row.get("success")]
    expected = case_config.get("expected", {})
    analysis = case_config.get("analysis", {})
    root_service = str(analysis.get("root_service", "gateway"))
    services = set(analysis.get("monitored_services", [])) or {root_service}

    representative = representative_metrics(eval_graph_rows, services)
    detection = detect_bottleneck(representative, topology, root_service, services)
    observed_edges = topology_edges(graph_rows)
    expected_edges = list(expected.get("graph_edges", []))

    service_metrics = {
        service: {
            "platform_p90_ms": round(metric_obj(representative, service).get("platform_p90_ms", 0.0), 3),
            "rps": round(metric_obj(representative, service).get("rps", 0.0), 3),
            "service_handling_latency": round(metric_obj(representative, service).get("service_handling_latency", 0.0), 3),
            "dependency_attributed_latency": round(metric_obj(representative, service).get("dependency_attributed_latency", 0.0), 3),
            "external_wait_latency": round(metric_obj(representative, service).get("external_wait_latency", 0.0), 3),
            "runq_p90_latency": round(metric_obj(representative, service).get("runq_p90_latency", 0.0), 3),
            "cpu_throttle_ratio": round(metric_obj(representative, service).get("cpu_throttle_ratio", 0.0), 4),
        }
        for service in sorted(services)
    }

    expected_reason = normalize_reason_class(str(expected.get("reason_class", "")))
    expected_service = str(expected.get("bottleneck_service", ""))

    snapshot_detections = []
    graph_hits = 0
    for row in eval_graph_rows:
        row_payload = row.get("payload") or {}
        row_topology = row_payload.get("topology", {}) if isinstance(row_payload, dict) else {}
        row_metrics = snapshot_metrics(row, services)
        det = detect_bottleneck(row_metrics, row_topology if isinstance(row_topology, dict) else {}, root_service, services)
        row_path_reason = normalize_reason_class(str(det.get("path_reason_class") or ""))
        row_leaf_reason = normalize_reason_class(str(det.get("leaf_reason_class") or ""))
        if expected_reason == "downstream_delay":
            row_evaluated_reason = row_path_reason or normalize_reason_class(str(det["reason_class"]))
            row_reason_scope = "path"
        else:
            row_evaluated_reason = row_leaf_reason or normalize_reason_class(str(det["reason_class"]))
            row_reason_scope = "leaf"
        snapshot_detections.append(
            {
                "service": str(det["bottleneck_service"]),
                "evaluated_reason": row_evaluated_reason,
                "path_reason": row_path_reason,
                "leaf_reason": row_leaf_reason,
                "reason_scope_used": row_reason_scope,
            }
        )
        if expected_edges_present(row, expected_edges):
            graph_hits += 1

    max_service_consecutive = 0
    max_reason_consecutive = 0
    current_service_consecutive = 0
    current_reason_consecutive = 0
    for row in snapshot_detections:
        if row["service"] == expected_service:
            current_service_consecutive += 1
        else:
            current_service_consecutive = 0
        if row["evaluated_reason"] == expected_reason:
            current_reason_consecutive += 1
        else:
            current_reason_consecutive = 0
        max_service_consecutive = max(max_service_consecutive, current_service_consecutive)
        max_reason_consecutive = max(max_reason_consecutive, current_reason_consecutive)

    path_reason = normalize_reason_class(str(detection.get("path_reason_class") or ""))
    leaf_reason = normalize_reason_class(str(detection.get("leaf_reason_class") or ""))
    detected_top_level_reason = top_level_reason_class(detection)
    if expected_reason in TOP_LEVEL_REASON_CLASSES:
        evaluated_reason = detected_top_level_reason
        reason_scope_used = "top_level"
    elif expected_reason == "downstream_delay":
        evaluated_reason = path_reason or normalize_reason_class(str(detection["reason_class"]))
        reason_scope_used = "path"
    else:
        evaluated_reason = leaf_reason or normalize_reason_class(str(detection["reason_class"]))
        reason_scope_used = "leaf"

    service_stability_pass = max_service_consecutive >= MIN_CONSECUTIVE_MATCH_LOOPS
    reason_stability_pass = max_reason_consecutive >= MIN_CONSECUTIVE_MATCH_LOOPS
    service_pass = str(detection["bottleneck_service"]) == expected_service
    reason_pass = expected_reason == evaluated_reason
    expected_path_reason = normalize_reason_class(str(expected.get("path_reason_class", "") or ""))
    expected_leaf_reason = normalize_reason_class(str(expected.get("leaf_reason_class", "") or ""))
    path_reason_pass = None if not expected_path_reason else (path_reason == expected_path_reason)
    leaf_reason_pass = None if not expected_leaf_reason else (leaf_reason == expected_leaf_reason)
    graph_pass = bool(eval_graph_rows) and graph_hits >= max(1, (len(eval_graph_rows) + 1) // 2)

    return {
        "case_name": case_config.get("case_name", case_dir.name),
        "phase_name": phase_name,
        "mode": collector_session.get("mode", case_config.get("mode", "observation")),
        "client": {
            "p90_latency_ms": round(p90(success_latencies), 3),
            "success_rps": round(safe_float(request_summary.get("success_rps", 0.0), 0.0), 3),
            "successful_responses": safe_int(request_summary.get("successful_responses", 0), 0),
            "failed_responses": safe_int(request_summary.get("failed_responses", 0), 0),
        },
        "graph": {
            "expected_edges": expected_edges,
            "observed_edges": observed_edges,
            "evaluation_window_seconds": EVALUATION_WINDOW_SECONDS,
            "matching_snapshots": graph_hits,
            "total_snapshots": len(eval_graph_rows),
            "pass": graph_pass,
        },
        "slo_setup": collector_session.get("slo_setup", {}),
        "platform": service_metrics,
        "detection": {
            "detected_bottleneck_service": detection["bottleneck_service"],
            "detected_reason_class": normalize_reason_class(str(detection["reason_class"])),
            "detected_top_level_reason_class": detected_top_level_reason,
            "detected_path_reason_class": path_reason,
            "detected_leaf_reason_class": leaf_reason,
            "evaluated_reason_class": evaluated_reason,
            "reason_scope_used": reason_scope_used,
            "reason_detail": detection["reason_detail"],
            "path": detection["path"],
        },
        "expected": {
            "bottleneck_service": expected.get("bottleneck_service", ""),
            "reason_class": expected_reason,
            "path_reason_class": expected_path_reason,
            "leaf_reason_class": expected_leaf_reason,
        },
        "pass_fail": {
            "service_pass": service_pass,
            "service_identification_pass": service_pass,
            "service_stability_pass": service_stability_pass,
            "reason_pass": reason_pass,
            "reason_stability_pass": reason_stability_pass,
            "path_reason_pass": path_reason_pass,
            "leaf_reason_pass": leaf_reason_pass,
            "max_service_consecutive_loops": max_service_consecutive,
            "max_reason_consecutive_loops": max_reason_consecutive,
            "overall_pass": service_pass and reason_pass and graph_pass,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize one bottleneck evaluation case.")
    parser.add_argument("--case-dir", required=True, type=Path, help="Case result directory")
    parser.add_argument("--phase-name", required=True, help="Phase label")
    args = parser.parse_args()

    summary = summarize(args.case_dir, args.phase_name)
    out_path = args.case_dir / "summary.json"
    out_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
