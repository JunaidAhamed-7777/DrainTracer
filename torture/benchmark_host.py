"""Measure the host-context sampler without starting or controlling a target."""

from __future__ import annotations

import argparse
import json
import math
import platform
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.leak_detector import LeakDetector
from src.process_runner import ProcessManager


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=200)
    args = parser.parse_args()
    if not 10 <= args.samples <= 2000:
        parser.error("--samples must be between 10 and 2000")
    detector = LeakDetector(ProcessManager(None))
    refresh, cached = [], []
    for _ in range(args.samples):
        detector._host_sampled_at = None
        start = time.perf_counter()
        detector._sample_host()
        refresh.append((time.perf_counter() - start) * 1000)
        start = time.perf_counter()
        detector._sample_host()
        cached.append((time.perf_counter() - start) * 1000)
    p99 = sorted(refresh)[math.ceil(len(refresh) * 0.99) - 1]
    budget = max(1, math.ceil(2 * p99))
    print(json.dumps({
        "os": platform.system(), "release": platform.release(), "python": platform.python_version(),
        "samples": args.samples, "interval_seconds": detector.HOST_INTERVAL_SECONDS,
        "refresh_ms": {"median": statistics.median(refresh), "p99": p99, "max": max(refresh)},
        "cached_ms": {"median": statistics.median(cached), "max": max(cached)},
        "local_refresh_budget_ms": budget,
        "budget_share_of_interval_percent": budget / (detector.HOST_INTERVAL_SECONDS * 10),
    }, indent=2))


if __name__ == "__main__":
    main()
