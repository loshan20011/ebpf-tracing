#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path
from statistics import quantiles


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


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def p90(values: list[float]) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    return float(quantiles(values, n=10, method="inclusive")[8])


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    n = len(values)
    mid = n // 2
    if n % 2:
        return float(values[mid])
    return float(values[mid - 1] + values[mid]) / 2.0


def metric_series(graph_rows: list[dict], service: str, key: str) -> list[float]:
    series = []
    for row in graph_rows:
        payload = row.get("payload") or {}
        metrics = payload.get("metrics", {}) if isinstance(payload, dict) else {}
        service_metrics = metrics.get(service, {}) if isinstance(metrics, dict) else {}
        try:
            value = float(service_metrics.get(key, 0.0) or 0.0)
        except Exception:
            value = 0.0
        series.append(value)
    return series


def service_metric_rows(graph_rows: list[dict], service: str) -> list[dict]:
    out = []
    for row in graph_rows:
        payload = row.get("payload") or {}
        metrics = payload.get("metrics", {}) if isinstance(payload, dict) else {}
        service_metrics = metrics.get(service, {}) if isinstance(metrics, dict) else {}
        if isinstance(service_metrics, dict):
            out.append(service_metrics)
    return out


def latency_validity(metric: dict) -> tuple[bool, str]:
    ebpf_req_corroborated = bool(metric.get("ebpf_req_corroborated", False))
    if ebpf_req_corroborated:
        return True, "corroborated_ebpf_request_evidence"
    if int(metric.get("ebpf_req_count", 0) or 0) > 0:
        return False, "uncorroborated_ebpf_request_evidence"
    return False, "no_request_evidence"


def valid_metric_series(graph_rows: list[dict], service: str, key: str) -> list[float]:
    series = []
    for metric in service_metric_rows(graph_rows, service):
        valid, _reason = latency_validity(metric)
        if not valid:
            continue
        try:
            series.append(float(metric.get(key, 0.0) or 0.0))
        except Exception:
            continue
    return series


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


def replica_snapshot(replica_rows: list[dict]) -> dict:
    latest = replica_rows[-1]["payload"] if replica_rows else {}
    items = latest.get("items", []) if isinstance(latest, dict) else []
    out = {}
    for item in items:
        name = item.get("metadata", {}).get("name")
        if not name:
            continue
        out[name] = {
            "spec_replicas": item.get("spec", {}).get("replicas", 0),
            "ready_replicas": item.get("status", {}).get("readyReplicas", 0),
            "available_replicas": item.get("status", {}).get("availableReplicas", 0),
        }
    return out


def summarize(case_dir: Path) -> dict:
    request_summary = json.loads((case_dir / "request_summary.json").read_text(encoding="utf-8"))
    session_meta = load_json(case_dir / "collector_session.json")
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
    audit_rows = filter_rows_by_window(load_ndjson(case_dir / "controller_audit.ndjson"), case_start_ms, case_end_ms)
    replica_rows = filter_rows_by_window(load_ndjson(case_dir / "replica_counts.ndjson"), case_start_ms, case_end_ms)

    success_latencies = [float(r.get("latency_ms", 0.0)) for r in request_rows if r.get("success")]
    client_duration = float(request_summary.get("duration_seconds", 0.0) or 0.0)
    successful_responses = int(request_summary.get("successful_responses", 0) or 0)

    latest_graph_payload = graph_rows[-1]["payload"] if graph_rows else {}
    latest_metrics = latest_graph_payload.get("metrics", {}) if isinstance(latest_graph_payload, dict) else {}

    monitored_services = ["front-end", "catalogue", "carts", "orders", "user", "payment", "shipping"]
    service_summary = {}
    runq_ranges = {}
    for service in monitored_services:
        valid_latency_values = valid_metric_series(graph_rows, service, "p90_latency")
        service_metrics = service_metric_rows(graph_rows, service)
        valid_reason = "no_samples"
        valid = False
        for metric in reversed(service_metrics):
            valid, valid_reason = latency_validity(metric)
            if valid:
                break
        service_summary[service] = {
            "platform_p90_median_ms": round(median(valid_latency_values), 3) if valid_latency_values else None,
            "platform_rps_median": round(median(metric_series(graph_rows, service, "rps")), 3),
            "latency_valid": bool(valid_latency_values),
            "latency_valid_reason": valid_reason if service_metrics else "no_samples",
        }
        runq_values = metric_series(graph_rows, service, "runq_p90_latency")
        if not any(runq_values):
            runq_values = metric_series(graph_rows, service, "avg_runq_latency")
        runq_ranges[service] = {
            "min_ms": round(min(runq_values) if runq_values else 0.0, 3),
            "median_ms": round(median(runq_values), 3),
            "max_ms": round(max(runq_values) if runq_values else 0.0, 3),
        }

    flattened_traces = payload_items_in_window(trace_rows, "traces", case_start_ms, case_end_ms)
    filtered_audit = payload_items_in_window(audit_rows, "events", case_start_ms, case_end_ms)

    scale_actions = [
        row for row in flattened_traces
        if isinstance(row, dict) and row.get("decision") in {"scale_up", "scale_down"}
    ]

    frontend_valid_latency_values = valid_metric_series(graph_rows, "front-end", "p90_latency")
    frontend_metrics = service_metric_rows(graph_rows, "front-end")
    frontend_valid = False
    frontend_valid_reason = "no_samples"
    for metric in reversed(frontend_metrics):
        frontend_valid, frontend_valid_reason = latency_validity(metric)
        if frontend_valid:
            break

    summary = {
        "case_name": request_summary.get("case_name"),
        "client": {
            "p90_latency_ms": round(p90(success_latencies), 3),
            "success_rps": round(successful_responses / max(client_duration, 0.001), 3),
            "sent_requests": int(request_summary.get("sent_requests", 0) or 0),
            "successful_responses": successful_responses,
            "failed_responses": int(request_summary.get("failed_responses", 0) or 0),
            "duration_seconds": round(client_duration, 3),
        },
        "platform": {
            "frontend_p90_median_ms": round(median(frontend_valid_latency_values), 3) if frontend_valid_latency_values else None,
            "frontend_rps_median": round(median(metric_series(graph_rows, "front-end", "rps")), 3),
            "frontend_latency_valid": bool(frontend_valid_latency_values),
            "frontend_latency_valid_reason": frontend_valid_reason if frontend_metrics else "no_samples",
            "frontend_client_comparison_valid": bool(frontend_valid_latency_values),
            "service_summary": service_summary,
            "runq_normal_range": runq_ranges,
            "observed_graph_edges": topology_edges(graph_rows),
            "latest_metrics_keys": sorted(latest_metrics.keys()) if isinstance(latest_metrics, dict) else [],
        },
        "controller": {
            "scale_action_count": len(scale_actions),
            "scale_actions": scale_actions,
            "trace_count": len(flattened_traces),
            "audit_count": len(filtered_audit),
        },
        "replicas": replica_snapshot(replica_rows),
        "window": {
            "start_unix_ms": case_start_ms,
            "end_unix_ms": case_end_ms,
            "collector_mode": session_meta.get("mode", ""),
        },
    }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize one functional evaluation case.")
    parser.add_argument("--case-dir", required=True, type=Path, help="Case result directory")
    args = parser.parse_args()

    summary = summarize(args.case_dir)
    out_path = args.case_dir / "summary.json"
    out_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
