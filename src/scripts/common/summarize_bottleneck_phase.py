#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def fmt(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def build_evidence(summary: dict) -> str:
    detection = summary.get("detection", {}) or {}
    platform = summary.get("platform", {}) or {}
    detected_service = str(detection.get("detected_bottleneck_service", "") or "")
    path = detection.get("path", []) or []

    service_metric = platform.get(detected_service, {}) if isinstance(platform, dict) else {}
    if not isinstance(service_metric, dict):
        service_metric = {}

    parts = []
    if path:
        parts.append(f"path={' -> '.join(path)}")
    if detected_service:
        parts.append(
            f"{detected_service}: handling={fmt(service_metric.get('service_handling_latency'))}ms, "
            f"dep={fmt(service_metric.get('dependency_attributed_latency'))}ms, "
            f"external={fmt(service_metric.get('external_wait_latency'))}ms, "
            f"runq={fmt(service_metric.get('runq_p90_latency'))}ms, "
            f"throttle={fmt(service_metric.get('cpu_throttle_ratio'))}"
        )

    return " | ".join(parts) if parts else "-"


def summarize_phase(results_root: Path) -> dict:
    rows = []
    for summary_path in sorted(results_root.glob("*/summary.json")):
        summary = load_json(summary_path)
        rows.append(
            {
                "case_name": summary.get("case_name", summary_path.parent.name),
                "trigger_service": summary.get("detection", {}).get("trigger_service", ""),
                "expected_service": summary.get("expected", {}).get("bottleneck_service", ""),
                "actual_service": summary.get("detection", {}).get("detected_bottleneck_service", ""),
                "expected_reason": summary.get("expected", {}).get("reason_class", ""),
                "actual_reason": summary.get("detection", {}).get("detected_reason_class", ""),
                "service_pass": bool(summary.get("pass_fail", {}).get("service_pass", False)),
                "reason_pass": bool(summary.get("pass_fail", {}).get("reason_pass", False)),
                "graph_pass": bool(summary.get("pass_fail", {}).get("graph_pass", False)),
                "stability_pass": bool(summary.get("pass_fail", {}).get("stability_pass", False)),
                "overall_pass": bool(summary.get("pass_fail", {}).get("overall_pass", False)),
                "evidence": build_evidence(summary),
            }
        )
    return {"results_root": str(results_root), "case_count": len(rows), "rows": rows}


def compact_table(rows: list[dict]) -> str:
    lines = [
        "| Case | Trigger | Expected Service | Detected Service | Detected Type | Graph | Result | Evidence |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['case_name']} | {row['trigger_service']} | {row['expected_service']} | {row['actual_service']} | "
            f"{row['actual_reason']} | {'PASS' if row['graph_pass'] else 'FAIL'} | "
            f"{'PASS' if row['overall_pass'] else 'FAIL'} | {row['evidence']} |"
        )
    return "\n".join(lines) + "\n"


def markdown_table(rows: list[dict]) -> str:
    lines = [
        "| Case | Trigger | Expected Service | Actual Service | Service Pass | Expected Type | Actual Type | Reason Pass | Graph Pass | Stable | Overall |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['case_name']} | {row['trigger_service']} | {row['expected_service']} | {row['actual_service']} | "
            f"{'PASS' if row['service_pass'] else 'FAIL'} | "
            f"{row['expected_reason']} | {row['actual_reason']} | {'PASS' if row['reason_pass'] else 'FAIL'} | "
            f"{'PASS' if row['graph_pass'] else 'FAIL'} | {'PASS' if row['stability_pass'] else 'FAIL'} | "
            f"{'PASS' if row['overall_pass'] else 'FAIL'} |"
        )
    return "\n".join(lines) + "\n"


def print_compact_report(rows: list[dict]) -> None:
    print("Summary Table:")
    print(compact_table(rows).strip())


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a short phase summary table for bottleneck tests.")
    parser.add_argument("--results-root", required=True, type=Path, help="Phase results root")
    args = parser.parse_args()

    summary = summarize_phase(args.results_root)
    (args.results_root / "phase_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (args.results_root / "phase_summary.md").write_text(
        markdown_table(summary["rows"]),
        encoding="utf-8",
    )
    (args.results_root / "phase_summary_compact.md").write_text(
        compact_table(summary["rows"]),
        encoding="utf-8",
    )
    print_compact_report(summary["rows"])
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
