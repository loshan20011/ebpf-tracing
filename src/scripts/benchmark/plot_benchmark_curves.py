#!/usr/bin/env python3
import argparse
import json
from collections import defaultdict
from pathlib import Path


ARM_ORDER = [
    ("noautoscale", "No Autoscaler", "#6b7280"),
    ("hpa50", "HPA-50", "#2563eb"),
    ("hpa70", "HPA-70", "#f59e0b"),
    ("thrivescale", "ThriveScale", "#16a34a"),
]

WIDTH = 900
HEIGHT = 560
PLOT_LEFT = 80
PLOT_RIGHT = 30
PLOT_TOP = 50
PLOT_BOTTOM = 80


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def phase_rps_at_time(phases: list[dict], start_s: float) -> float:
    elapsed = 0.0
    for phase in phases:
        duration = float(phase.get("duration_seconds", 0) or 0)
        if start_s < elapsed + duration:
            return float(phase.get("rps", 0) or 0)
        elapsed += duration
    if phases:
        return float(phases[-1].get("rps", 0) or 0)
    return 0.0


def aggregate_violation_by_rps(summary: dict) -> tuple[list[float], list[float]]:
    phases = summary.get("phases", []) or []
    buckets: dict[float, dict[str, float]] = defaultdict(lambda: {"violating": 0.0, "total": 0.0})
    for window in summary.get("client_windows", []) or []:
        rps = phase_rps_at_time(phases, float(window.get("start_s", 0.0) or 0.0))
        buckets[rps]["total"] += 1.0
        if bool(window.get("slo_violating", False)):
            buckets[rps]["violating"] += 1.0
    xs = sorted(buckets)
    ys = [(buckets[x]["violating"] / buckets[x]["total"]) * 100.0 if buckets[x]["total"] else 0.0 for x in xs]
    return xs, ys


def aggregate_replica_delta_by_rps(summary: dict) -> tuple[list[float], list[float]]:
    phases = summary.get("phases", []) or []
    buckets: dict[float, list[float]] = defaultdict(list)
    previous_total = None
    for window in summary.get("replica_windows", []) or []:
        total = float(window.get("total_replicas", 0.0) or 0.0)
        delta = 0.0 if previous_total is None else total - previous_total
        previous_total = total
        rps = phase_rps_at_time(phases, float(window.get("start_s", 0.0) or 0.0))
        buckets[rps].append(delta)
    xs = sorted(buckets)
    ys = [sum(buckets[x]) / len(buckets[x]) if buckets[x] else 0.0 for x in xs]
    return xs, ys


def svg_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def to_points(xs: list[float], ys: list[float], min_x: float, max_x: float, min_y: float, max_y: float) -> list[tuple[float, float]]:
    plot_width = WIDTH - PLOT_LEFT - PLOT_RIGHT
    plot_height = HEIGHT - PLOT_TOP - PLOT_BOTTOM
    x_span = max(max_x - min_x, 1.0)
    y_span = max(max_y - min_y, 1.0)
    points = []
    for x, y in zip(xs, ys):
        px = PLOT_LEFT + ((x - min_x) / x_span) * plot_width
        py = PLOT_TOP + plot_height - ((y - min_y) / y_span) * plot_height
        points.append((px, py))
    return points


def axis_ticks(min_value: float, max_value: float, count: int = 5) -> list[float]:
    if max_value <= min_value:
        return [min_value]
    step = (max_value - min_value) / count
    return [min_value + step * i for i in range(count + 1)]


def write_svg(output_path: Path, title: str, ylabel: str, all_series: list[tuple[str, str, list[float], list[float]]]) -> None:
    non_empty = [(label, color, xs, ys) for label, color, xs, ys in all_series if xs]
    if not non_empty:
        raise SystemExit(f"no benchmark series found for {output_path}")

    all_x = [x for _label, _color, xs, _ys in non_empty for x in xs]
    all_y = [y for _label, _color, _xs, ys in non_empty for y in ys]

    min_x = min(all_x)
    max_x = max(all_x)
    min_y = min(0.0, min(all_y))
    max_y = max(all_y)
    if max_y <= min_y:
        max_y = min_y + 1.0

    x_ticks = sorted(set(all_x))
    y_ticks = axis_ticks(min_y, max_y, 5)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{WIDTH/2}" y="28" text-anchor="middle" font-size="22" font-family="Arial">{svg_escape(title)}</text>',
    ]

    plot_width = WIDTH - PLOT_LEFT - PLOT_RIGHT
    plot_height = HEIGHT - PLOT_TOP - PLOT_BOTTOM

    for tick in y_ticks:
        y = PLOT_TOP + plot_height - ((tick - min_y) / max(max_y - min_y, 1.0)) * plot_height
        parts.append(f'<line x1="{PLOT_LEFT}" y1="{y:.2f}" x2="{WIDTH - PLOT_RIGHT}" y2="{y:.2f}" stroke="#e5e7eb" stroke-width="1"/>')
        parts.append(f'<text x="{PLOT_LEFT - 10}" y="{y + 4:.2f}" text-anchor="end" font-size="12" font-family="Arial" fill="#374151">{tick:.2f}</text>')

    for tick in x_ticks:
        x = PLOT_LEFT + ((tick - min_x) / max(max_x - min_x, 1.0)) * plot_width
        parts.append(f'<line x1="{x:.2f}" y1="{PLOT_TOP}" x2="{x:.2f}" y2="{HEIGHT - PLOT_BOTTOM}" stroke="#f3f4f6" stroke-width="1"/>')
        parts.append(f'<text x="{x:.2f}" y="{HEIGHT - PLOT_BOTTOM + 22}" text-anchor="middle" font-size="12" font-family="Arial" fill="#374151">{tick:.0f}</text>')

    parts.append(f'<line x1="{PLOT_LEFT}" y1="{PLOT_TOP}" x2="{PLOT_LEFT}" y2="{HEIGHT - PLOT_BOTTOM}" stroke="#111827" stroke-width="1.5"/>')
    parts.append(f'<line x1="{PLOT_LEFT}" y1="{HEIGHT - PLOT_BOTTOM}" x2="{WIDTH - PLOT_RIGHT}" y2="{HEIGHT - PLOT_BOTTOM}" stroke="#111827" stroke-width="1.5"/>')
    parts.append(f'<text x="{WIDTH/2}" y="{HEIGHT - 20}" text-anchor="middle" font-size="16" font-family="Arial">Request Rate (RPS)</text>')
    parts.append(
        f'<text x="24" y="{HEIGHT/2}" text-anchor="middle" font-size="16" font-family="Arial" transform="rotate(-90 24 {HEIGHT/2})">{svg_escape(ylabel)}</text>'
    )

    legend_x = PLOT_LEFT
    legend_y = HEIGHT - 32
    for idx, (label, color, xs, ys) in enumerate(non_empty):
        x = legend_x + idx * 190
        parts.append(f'<line x1="{x}" y1="{legend_y}" x2="{x + 24}" y2="{legend_y}" stroke="{color}" stroke-width="3"/>')
        parts.append(f'<text x="{x + 32}" y="{legend_y + 4}" font-size="13" font-family="Arial" fill="#111827">{svg_escape(label)}</text>')
        points = to_points(xs, ys, min_x, max_x, min_y, max_y)
        polyline = " ".join(f"{px:.2f},{py:.2f}" for px, py in points)
        parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="3" points="{polyline}"/>')
        for px, py in points:
            parts.append(f'<circle cx="{px:.2f}" cy="{py:.2f}" r="4" fill="{color}"/>')

    parts.append("</svg>")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(parts) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Plot benchmark comparison curves from benchmark summaries.")
    parser.add_argument(
        "--results-root",
        default="results/benchmark/login",
        help="Directory containing per-arm benchmark result folders.",
    )
    args = parser.parse_args()

    results_root = Path(args.results_root)
    violation_series = []
    replica_series = []
    for folder, label, color in ARM_ORDER:
        summary_path = results_root / folder / "summary.json"
        if not summary_path.exists():
            continue
        summary = load_json(summary_path)
        vx, vy = aggregate_violation_by_rps(summary)
        rx, ry = aggregate_replica_delta_by_rps(summary)
        violation_series.append((label, color, vx, vy))
        replica_series.append((label, color, rx, ry))

    write_svg(
        results_root / "violation_rate_vs_rps.svg",
        "SLO Violation Rate vs Request Rate",
        "Violation Rate (%)",
        violation_series,
    )
    write_svg(
        results_root / "replica_delta_vs_rps.svg",
        "Average Replica Change vs Request Rate",
        "Average Replica Delta per Window",
        replica_series,
    )


if __name__ == "__main__":
    main()
