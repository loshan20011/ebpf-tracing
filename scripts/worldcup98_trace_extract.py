#!/usr/bin/env python3
import argparse
import csv
import gzip
import math
import struct
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Tuple


BASE_URL = "https://ita.ee.lbl.gov/traces/WorldCup"
REQUEST_STRUCT = struct.Struct(">IIII4B")


@dataclass
class TraceBucket:
    bucket_index: int
    epoch_start: int
    requests: int
    rps: float


def download_day_parts(day: int, cache_dir: Path) -> List[Path]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    parts: List[Path] = []

    for part in range(1, 64):
        name = f"wc_day{day}_{part}.gz"
        url = f"{BASE_URL}/{name}"
        target = cache_dir / name
        if target.exists():
            parts.append(target)
            continue

        try:
            with urllib.request.urlopen(url) as resp:
                data = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                break
            raise

        target.write_bytes(data)
        parts.append(target)

    if not parts:
        raise SystemExit(f"No WorldCup98 files found for day {day}")

    return parts


def iter_timestamps(paths: Iterable[Path]) -> Iterable[int]:
    for path in paths:
        with gzip.open(path, "rb") as fh:
            while True:
                chunk = fh.read(REQUEST_STRUCT.size)
                if not chunk:
                    break
                if len(chunk) != REQUEST_STRUCT.size:
                    raise SystemExit(f"Corrupt record length in {path}")
                timestamp, *_ = REQUEST_STRUCT.unpack(chunk)
                yield int(timestamp)


def bucketize_timestamps(timestamps: Iterable[int], bucket_seconds: int) -> List[TraceBucket]:
    counts = Counter()
    min_bucket = None
    max_bucket = None

    for ts in timestamps:
        bucket = int(ts) // bucket_seconds
        counts[bucket] += 1
        if min_bucket is None or bucket < min_bucket:
            min_bucket = bucket
        if max_bucket is None or bucket > max_bucket:
            max_bucket = bucket

    if min_bucket is None or max_bucket is None:
        raise SystemExit("No timestamps were parsed from the trace files")

    buckets: List[TraceBucket] = []
    for bucket in range(min_bucket, max_bucket + 1):
        requests = int(counts.get(bucket, 0))
        buckets.append(
            TraceBucket(
                bucket_index=bucket,
                epoch_start=bucket * bucket_seconds,
                requests=requests,
                rps=requests / float(bucket_seconds),
            )
        )
    return buckets


def pick_peak_window(buckets: List[TraceBucket], window_seconds: int, bucket_seconds: int) -> Tuple[int, int]:
    window_buckets = max(1, int(math.ceil(window_seconds / float(bucket_seconds))))
    peak_index = max(range(len(buckets)), key=lambda idx: buckets[idx].requests)
    half = window_buckets // 2
    start = max(0, peak_index - half)
    end = start + window_buckets
    if end > len(buckets):
        end = len(buckets)
        start = max(0, end - window_buckets)
    return start, end


def write_csv(buckets: List[TraceBucket], out_path: Path, day: int, bucket_seconds: int, source_files: List[Path]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "bucket_offset_s",
                "bucket_epoch_start",
                "requests",
                "rps",
                "day",
                "bucket_seconds",
                "source_parts",
            ]
        )
        first_epoch = buckets[0].epoch_start if buckets else 0
        joined = ",".join(path.name for path in source_files)
        for bucket in buckets:
            writer.writerow(
                [
                    bucket.epoch_start - first_epoch,
                    bucket.epoch_start,
                    bucket.requests,
                    f"{bucket.rps:.6f}",
                    day,
                    bucket_seconds,
                    joined,
                ]
            )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Download and extract a real WorldCup98 trace slice for Sock Shop replay."
    )
    p.add_argument("--day", type=int, default=75, help="WorldCup98 day number to extract")
    p.add_argument("--bucket-seconds", type=int, default=10, help="Aggregation bucket size in seconds")
    p.add_argument(
        "--window-seconds",
        type=int,
        default=600,
        help="Length of the extracted peak window in seconds",
    )
    p.add_argument(
        "--cache-dir",
        default="deploy/03-evaluation/datasets/raw/worldcup98",
        help="Directory used to cache downloaded raw .gz trace parts",
    )
    p.add_argument(
        "--output",
        default="deploy/03-evaluation/datasets/worldcup98_day75_peak_10m_10s.csv",
        help="Output CSV path for the extracted real trace window",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    cache_dir = Path(args.cache_dir)
    output = Path(args.output)

    parts = download_day_parts(args.day, cache_dir)
    buckets = bucketize_timestamps(iter_timestamps(parts), int(args.bucket_seconds))
    start, end = pick_peak_window(buckets, int(args.window_seconds), int(args.bucket_seconds))
    selected = buckets[start:end]

    write_csv(selected, output, int(args.day), int(args.bucket_seconds), parts)

    peak_rps = max((bucket.rps for bucket in selected), default=0.0)
    avg_rps = sum(bucket.rps for bucket in selected) / max(1, len(selected))
    print(f"[ok] wrote {output}")
    print(f"[info] day={args.day} parts={len(parts)} bucket_seconds={args.bucket_seconds} window_seconds={args.window_seconds}")
    print(f"[info] selected_buckets={len(selected)} avg_rps={avg_rps:.3f} peak_rps={peak_rps:.3f}")


if __name__ == "__main__":
    main()
