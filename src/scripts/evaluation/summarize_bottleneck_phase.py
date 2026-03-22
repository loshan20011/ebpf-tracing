#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def summarize_phase(results_root: Path) -> dict:
    rows = []
    for summary_path in sorted(results_root.glob("*/summary.json")):
        summary = load_json(summary_path)
        rows.append(
            {
                "case_name": summary.get("case_name", summary_path.parent.name),
                "expected_service": summary.get("expected", {}).get("bottleneck_service", ""),
                "actual_service": summary.get("detection", {}).get("detected_bottleneck_service", ""),
                "expected_reason": summary.get("expected", {}).get("reason_class", ""),
                "expected_path_reason": summary.get("expected", {}).get("path_reason_class", ""),
                "expected_leaf_reason": summary.get("expected", {}).get("leaf_reason_class", ""),
                "actual_reason": summary.get("detection", {}).get("detected_reason_class", ""),
                "path_reason": summary.get("detection", {}).get("detected_path_reason_class", ""),
                "leaf_reason": summary.get("detection", {}).get("detected_leaf_reason_class", ""),
                "evaluated_reason": summary.get("detection", {}).get("evaluated_reason_class", ""),
                "reason_scope_used": summary.get("detection", {}).get("reason_scope_used", ""),
                "service_pass": bool(summary.get("pass_fail", {}).get("service_pass", False)),
                "service_stability_pass": bool(summary.get("pass_fail", {}).get("service_stability_pass", False)),
                "reason_pass": bool(summary.get("pass_fail", {}).get("reason_pass", False)),
                "reason_stability_pass": bool(summary.get("pass_fail", {}).get("reason_stability_pass", False)),
                "path_reason_pass": summary.get("pass_fail", {}).get("path_reason_pass"),
                "leaf_reason_pass": summary.get("pass_fail", {}).get("leaf_reason_pass"),
                "overall_pass": bool(summary.get("pass_fail", {}).get("overall_pass", False)),
            }
        )
    return {"results_root": str(results_root), "case_count": len(rows), "rows": rows}


def markdown_table(rows: list[dict]) -> str:
    lines = [
        "| Case | Expected Service | Actual Service | Service Pass | Service Stable | Expected Reason | Evaluated Reason | Reason Pass | Reason Stable | Expected Path | Path Reason | Path Pass | Expected Leaf | Leaf Reason | Leaf Pass | Overall |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['case_name']} | {row['expected_service']} | {row['actual_service']} | "
            f"{'PASS' if row['service_pass'] else 'FAIL'} | "
            f"{'PASS' if row['service_stability_pass'] else 'FAIL'} | "
            f"{row['expected_reason']} | {row['evaluated_reason']} | {'PASS' if row['reason_pass'] else 'FAIL'} | "
            f"{'PASS' if row['reason_stability_pass'] else 'FAIL'} | "
            f"{row['expected_path_reason']} | {row['path_reason']} | {'' if row['path_reason_pass'] is None else ('PASS' if row['path_reason_pass'] else 'FAIL')} | "
            f"{row['expected_leaf_reason']} | {row['leaf_reason']} | {'' if row['leaf_reason_pass'] is None else ('PASS' if row['leaf_reason_pass'] else 'FAIL')} | "
            f"{'PASS' if row['overall_pass'] else 'FAIL'} |"
        )
    return "\n".join(lines) + "\n"


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
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
