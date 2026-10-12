"""Create repeatable handle/thread spikes that fully recover between bursts."""

from __future__ import annotations

import argparse
import tempfile
import threading
import time
from pathlib import Path
from typing import BinaryIO


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


def _wait_for_release(release: threading.Event) -> None:
    release.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=_positive_float, default=20.0)
    parser.add_argument("--burst-handles", type=_non_negative_int, default=200)
    parser.add_argument("--burst-threads", type=_non_negative_int, default=12)
    parser.add_argument("--hold-seconds", type=_positive_float, default=0.35)
    parser.add_argument("--recovery-seconds", type=_positive_float, default=0.65)
    args = parser.parse_args()

    deadline = time.monotonic() + args.duration
    cycle = 0
    with tempfile.TemporaryDirectory(prefix="draintracer-burst-") as temp_dir:
        path = Path(temp_dir) / "burst.bin"
        path.touch()

        while time.monotonic() < deadline:
            cycle += 1
            release = threading.Event()
            handles: list[BinaryIO] = [
                path.open("a+b", buffering=0) for _ in range(args.burst_handles)
            ]
            workers = [
                threading.Thread(
                    target=_wait_for_release,
                    args=(release,),
                    daemon=True,
                )
                for _ in range(args.burst_threads)
            ]
            for worker in workers:
                worker.start()

            print(
                f"burst_recovery: cycle {cycle} peak "
                f"({len(handles)} files, {len(workers)} workers)",
                flush=True,
            )
            time.sleep(
                min(args.hold_seconds, max(0.0, deadline - time.monotonic()))
            )

            for handle in handles:
                handle.close()
            release.set()
            for worker in workers:
                worker.join(timeout=1.0)

            print(f"burst_recovery: cycle {cycle} recovered", flush=True)
            time.sleep(
                min(
                    args.recovery_seconds,
                    max(0.0, deadline - time.monotonic()),
                )
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
