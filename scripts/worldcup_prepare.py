#!/usr/bin/env python3
import argparse
import csv
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional


DEFAULT_ROUTES = [
    {"name": "home", "group": "browse", "method": "GET", "path": "/", "weight": 18},
    {"name": "category", "group": "browse", "method": "GET", "path": "/category.html", "weight": 12},
    {"name": "catalogue", "group": "browse", "method": "GET", "path": "/catalogue", "weight": 34},
    {
        "name": "product_a",
        "group": "browse",
        "method": "GET",
        "path": "/detail.html?id=3395a43e-2d88-40de-b95f-e00e1502085b",
        "weight": 10,
    },
    {
        "name": "product_b",
        "group": "browse",
        "method": "GET",
        "path": "/detail.html?id=510a0d7e-8e83-4193-b483-e27e09ddc34d",
        "weight": 10,
    },
    {"name": "basket", "group": "cart", "method": "GET", "path": "/basket.html", "weight": 10},
    {"name": "customer_orders", "group": "checkout", "method": "GET", "path": "/customer-orders.html", "weight": 6},
]

VALUE_COLUMNS = ("rps", "rate", "requests", "count", "reqs", "hits", "value")


@dataclass
class Phase:
    start_s: int
    end_s: int
    target_rps: float


def parse_numeric(value: object) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except Exception:
        return None


def detect_value_column(fieldnames: Iterable[str]) -> Optional[str]:
    lowered = {str(name).strip().lower(): str(name) for name in fieldnames if str(name).strip()}
    for candidate in VALUE_COLUMNS:
        if candidate in lowered:
            return lowered[candidate]
    return None


def load_trace_values(path: Path, source_interval_seconds: float) -> List[float]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise SystemExit(f"No usable rows found in {path}")

    values: List[float] = []

    if "," not in lines[0]:
        for line in lines:
            value = parse_numeric(line)
            if value is not None:
                values.append(value)
        if not values:
            raise SystemExit(f"Could not parse numeric values from {path}")
        return values

    with path.open("r", encoding="utf-8", errors="ignore", newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        # Prefer the normal CSV comma dialect when the file clearly looks like
        # comma-separated data. csv.Sniffer can mis-detect underscores in field
        # names as delimiters for small samples, which breaks real trace files.
        first_line = lines[0]
        if "," in first_line and "\t" not in first_line and ";" not in first_line:
            dialect = csv.excel
        else:
            try:
                dialect = csv.Sniffer().sniff(sample)
            except Exception:
                dialect = csv.excel

        try:
            has_header = csv.Sniffer().has_header(sample)
        except Exception:
            has_header = True

        if has_header:
            reader = csv.DictReader(fh, dialect=dialect)
            value_column = detect_value_column(reader.fieldnames or [])
            if value_column is None:
                raise SystemExit(
                    f"Could not find a value column in {path}. Expected one of: {', '.join(VALUE_COLUMNS)}"
                )
            for row in reader:
                value = parse_numeric(row.get(value_column))
                if value is not None:
                    values.append(value)
        else:
            reader = csv.reader(fh, dialect=dialect)
            for row in reader:
                picked = None
                for cell in row:
                    picked = parse_numeric(cell)
                    if picked is not None:
                        break
                if picked is not None:
                    values.append(picked)

    if not values:
        raise SystemExit(f"Could not parse usable values from {path}")

    return values


def convert_to_rps(values: List[float], source_interval_seconds: float, values_are_rps: bool) -> List[float]:
    if values_are_rps:
        return [max(0.0, float(v)) for v in values]
    interval = max(1e-6, float(source_interval_seconds))
    return [max(0.0, float(v) / interval) for v in values]


def scale_trace(rps_values: List[float], target_peak_rps: float) -> tuple[List[float], float]:
    peak = max(rps_values) if rps_values else 0.0
    if peak <= 0.0 or target_peak_rps <= 0.0:
        return list(rps_values), 1.0
    factor = float(target_peak_rps) / float(peak)
    return [v * factor for v in rps_values], factor


def compress_phases(values: List[float], bucket_seconds: int, min_rps: float, repeat: int) -> List[Phase]:
    out: List[Phase] = []
    current = 0
    expanded = values * max(1, int(repeat))
    for raw in expanded:
        rps = max(float(min_rps), float(raw))
        rps = round(rps, 3)
        if out and math.isclose(out[-1].target_rps, rps, rel_tol=0.0, abs_tol=1e-9):
            out[-1].end_s += int(bucket_seconds)
        else:
            out.append(Phase(start_s=current, end_s=current + int(bucket_seconds), target_rps=rps))
        current += int(bucket_seconds)
    return out


def render_yaml(
    phases: List[Phase],
    source_file: Path,
    source_interval_seconds: float,
    bucket_seconds: int,
    peak_input_rps: float,
    scaled_peak_rps: float,
    scale_factor: float,
) -> str:
    lines: List[str] = []
    generated = datetime.now(timezone.utc).isoformat()
    lines.append("metadata:")
    lines.append(f"  generated_at_utc: \"{generated}\"")
    lines.append(f"  source_file: \"{source_file.name}\"")
    lines.append("  source_dataset: \"worldcup-like trace\"")
    lines.append(f"  source_interval_seconds: {float(source_interval_seconds):.3f}")
    lines.append(f"  bucket_seconds: {int(bucket_seconds)}")
    lines.append(f"  peak_input_rps: {round(float(peak_input_rps), 3)}")
    lines.append(f"  scaled_peak_rps: {round(float(scaled_peak_rps), 3)}")
    lines.append(f"  scale_factor: {round(float(scale_factor), 6)}")
    lines.append("  notes:")
    lines.append("    - \"Generated for Sock Shop replay using a World Cup-style trace shape.\"")
    lines.append("    - \"Replay should be compared under identical SLOs and CPU requests/limits for HPA vs ThriveScale.\"")
    lines.append("phases:")
    for phase in phases:
        lines.append(f"  - start_s: {phase.start_s}")
        lines.append(f"    end_s: {phase.end_s}")
        lines.append(f"    target_rps: {phase.target_rps}")
    lines.append("routes:")
    for route in DEFAULT_ROUTES:
        lines.append(f"  - name: {route['name']}")
        lines.append(f"    group: {route['group']}")
        lines.append(f"    method: {route['method']}")
        lines.append(f"    path: {route['path']}")
        lines.append(f"    weight: {route['weight']}")
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Prepare a scaled World Cup-style Sock Shop workload profile for ThriveScale evaluation."
    )
    p.add_argument("--input", required=True, help="Path to the raw trace file (CSV or one numeric value per line)")
    p.add_argument("--output", required=True, help="Output YAML workload profile path")
    p.add_argument(
        "--source-interval-seconds",
        type=float,
        default=60.0,
        help="Original trace bucket size in seconds when the values are request counts",
    )
    p.add_argument(
        "--values-are-rps",
        action="store_true",
        help="Treat input values as already being in requests-per-second form",
    )
    p.add_argument(
        "--bucket-seconds",
        type=int,
        default=60,
        help="Replay bucket size in seconds for each generated phase",
    )
    p.add_argument(
        "--target-peak-rps",
        type=float,
        default=180.0,
        help="Scale the trace so its maximum replay rate matches this peak RPS",
    )
    p.add_argument("--min-rps", type=float, default=1.0, help="Clamp every replay bucket to at least this RPS")
    p.add_argument("--repeat", type=int, default=1, help="Repeat the whole prepared trace this many times")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    src = Path(args.input)
    out = Path(args.output)
    if not src.exists():
        raise SystemExit(f"Input trace file not found: {src}")

    raw_values = load_trace_values(src, args.source_interval_seconds)
    source_rps = convert_to_rps(raw_values, args.source_interval_seconds, args.values_are_rps)
    scaled_rps, scale_factor = scale_trace(source_rps, args.target_peak_rps)
    phases = compress_phases(
        values=scaled_rps,
        bucket_seconds=int(args.bucket_seconds),
        min_rps=float(args.min_rps),
        repeat=int(args.repeat),
    )

    peak_input = max(source_rps) if source_rps else 0.0
    peak_scaled = max((p.target_rps for p in phases), default=0.0)
    yaml_text = render_yaml(
        phases=phases,
        source_file=src,
        source_interval_seconds=float(args.source_interval_seconds),
        bucket_seconds=int(args.bucket_seconds),
        peak_input_rps=peak_input,
        scaled_peak_rps=peak_scaled,
        scale_factor=scale_factor,
    )

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml_text, encoding="utf-8")

    duration = max((p.end_s for p in phases), default=0)
    print(f"[ok] wrote {out}")
    print(f"[info] source buckets={len(raw_values)} duration_s={duration} peak_input_rps={peak_input:.3f}")
    print(f"[info] scaled_peak_rps={peak_scaled:.3f} scale_factor={scale_factor:.6f}")


if __name__ == "__main__":
    main()
