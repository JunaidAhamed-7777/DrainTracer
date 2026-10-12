"""Bounded steady-state workload that should not produce a leak signal."""

from __future__ import annotations

import argparse
import tempfile
import threading
import time
from pathlib import Path


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be zero or greater")
    return parsed


def _cpu_worker(stop: threading.Event) -> None:
    value = 1
    while not stop.wait(0.02):
        for number in range(2_000):
            value = ((value << 5) - value + number) & 0xFFFFFFFF


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=_positive_float, default=20.0)
    parser.add_argument("--memory-mb", type=_non_negative_int, default=8)
    parser.add_argument("--handles", type=_non_negative_int, default=4)
    parser.add_argument("--threads", type=_non_negative_int, default=2)
    args = parser.parse_args()

    memory = [bytearray(1024 * 1024) for _ in range(args.memory_mb)]
    for block in memory:
        for offset in range(0, len(block), 4096):
            block[offset] = 1

    stop = threading.Event()
    workers = [
        threading.Thread(target=_cpu_worker, args=(stop,), daemon=True)
        for _ in range(args.threads)
    ]

    with tempfile.TemporaryDirectory(prefix="draintracer-steady-") as temp_dir:
        path = Path(temp_dir) / "held-open.bin"
        handles = [path.open("a+b", buffering=0) for _ in range(args.handles)]
        for worker in workers:
            worker.start()

        try:
            deadline = time.monotonic() + args.duration
            while time.monotonic() < deadline:
                print(
                    f"steady: {len(memory)} MiB, {len(handles)} files, "
                    f"{len(workers)} workers",
                    flush=True,
                )
                time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        finally:
            stop.set()
            for worker in workers:
                worker.join(timeout=1.0)
            for handle in handles:
                handle.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
