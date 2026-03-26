#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


ARMS = [
    ("noautoscale", "No Autoscaler"),
    ("hpa50", "HPA-50"),
    ("hpa70", "HPA-70"),
    ("thrivescale", "ThriveScale"),
]


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def fmt(value):
    if value is None or value == "":
        return "-"
    return str(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize all Sock Shop autoscaler scenario results.")
    parser.add_argument("--results-root", required=True, type=Path)
    args = parser.parse_args()

    rows = []
    for arm_key, arm_label in ARMS:
        arm_root = args.results_root / arm_key
        if not arm_root.exists():
            continue
        for case_dir in sorted(p for p in arm_root.iterdir() if p.is_dir()):
            summary = load_json(case_dir / "scenario_summary.json")
            if not summary:
                continue
            rows.append(
                {
                    "Autoscaler": arm_label,
                    "Scenario": summary.get("case_name", case_dir.name),
                    "Expected": summary.get("expected_service") or summary.get("expected_reason_class"),
                    "First scaled service": summary.get("first_scaled_service"),
                    "First action time (s)": summary.get("first_action_time_seconds"),
                    "Scaling magnitude": summary.get("scaling_magnitude_total"),
                    "Correct scale target": summary.get("correct_scale_target"),
                    "Correct no-scale behavior": summary.get("correct_no_scale_behavior"),
                    "Decision quality": summary.get("decision_quality"),
                }
            )

    lines = [
        "# Sock Shop Scenario-Based Autoscaling Comparison",
        "",
        "| Autoscaler | Scenario | Expected | First scaled service | First action time (s) | Scaling magnitude | Correct scale target | Correct no-scale behavior | Decision quality |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {fmt(row['Autoscaler'])} | {fmt(row['Scenario'])} | {fmt(row['Expected'])} | {fmt(row['First scaled service'])} | "
            f"{fmt(row['First action time (s)'])} | {fmt(row['Scaling magnitude'])} | {fmt(row['Correct scale target'])} | "
            f"{fmt(row['Correct no-scale behavior'])} | {fmt(row['Decision quality'])} |"
        )

    (args.results_root / "scenario_comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (args.results_root / "scenario_comparison.json").write_text(json.dumps(rows, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(rows, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
