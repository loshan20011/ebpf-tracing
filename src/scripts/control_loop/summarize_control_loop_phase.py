#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def case_dirs(results_root: Path) -> list[Path]:
    return sorted(path for path in results_root.iterdir() if path.is_dir())


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize a control-loop evaluation phase.")
    parser.add_argument("--results-root", required=True, type=Path, help="Phase result directory")
    args = parser.parse_args()

    rows = []
    for case_dir in case_dirs(args.results_root):
        summary_path = case_dir / "summary.json"
        if not summary_path.exists():
            continue
        summary = load_json(summary_path)
        rows.append(
            {
                "case_name": summary.get("case_name"),
                "expected_pattern_behavior": summary.get("expected", {}).get("pattern_behavior", ""),
                "expected_controller_behavior": summary.get("expected", {}).get("controller_behavior", ""),
                "expected_replica_trend": summary.get("expected", {}).get("replica_trend", ""),
                "expected_slo_behavior": summary.get("expected", {}).get("slo_behavior", ""),
                "actual_pattern_behavior": summary.get("control_loop", {}).get("actual_pattern_behavior", ""),
                "actual_final_replica_behavior": summary.get("control_loop", {}).get("final_replica_behavior", ""),
                "actual_slo_protection_result": summary.get("control_loop", {}).get("slo_protection_result", ""),
                "dominant_target_service": summary.get("control_loop", {}).get("dominant_target_service", ""),
                "scale_action_count": summary.get("control_loop", {}).get("scale_action_count"),
                "actual_replica_change_count": summary.get("control_loop", {}).get("actual_replica_change_count"),
                "oscillation_count": summary.get("control_loop", {}).get("oscillation_count"),
                "peak_replicas": summary.get("control_loop", {}).get("peak_replicas"),
                "capacity_ceiling_reached": summary.get("control_loop", {}).get("capacity_ceiling_reached"),
                "recovered_below_slo": summary.get("control_loop", {}).get("recovered_below_slo"),
                "pattern_pass": summary.get("pass_fail", {}).get("pattern_pass"),
                "replica_pass": summary.get("pass_fail", {}).get("replica_pass"),
                "slo_pass": summary.get("pass_fail", {}).get("slo_pass"),
                "overall_pass": summary.get("pass_fail", {}).get("overall_pass"),
            }
        )

    phase_summary = {
        "case_count": len(rows),
        "results_root": str(args.results_root),
        "rows": rows,
    }
    (args.results_root / "phase_summary.json").write_text(
        json.dumps(phase_summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    lines = [
        "| Case | Dominant Target | Expected Behavior | Actual Behavior | Replica Behavior | SLO Result | Proposals | Replica Changes | Ceiling | Recovered | Oscillation | Peak Replicas | Overall |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            "| {case} | {target} | {expected_behavior} | {actual_pattern} | {replica} | {slo} | {actions} | {changes} | {ceiling} | {recovered} | {oscillation} | {peak} | {overall} |".format(
                case=row["case_name"],
                target=row["dominant_target_service"],
                expected_behavior="; ".join(
                    part for part in [
                        row["expected_pattern_behavior"],
                        row["expected_controller_behavior"],
                        row["expected_replica_trend"],
                        row["expected_slo_behavior"],
                    ] if part
                ),
                actual_pattern=row["actual_pattern_behavior"],
                replica=row["actual_final_replica_behavior"],
                slo=row["actual_slo_protection_result"],
                actions=row["scale_action_count"],
                changes=row["actual_replica_change_count"],
                ceiling="yes" if row["capacity_ceiling_reached"] else "no",
                recovered="yes" if row["recovered_below_slo"] else "no",
                oscillation=row["oscillation_count"],
                peak=row["peak_replicas"],
                overall="PASS" if row["overall_pass"] else "FAIL",
            )
        )

    (args.results_root / "phase_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(phase_summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
