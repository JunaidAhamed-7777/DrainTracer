"""Open file handles monotonically while keeping a strict safety cap."""

from __future__ import annotations

import argparse
import tempfile
import time
from pathlib import Path
from typing import BinaryIO


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=_positive_float, default=20.0)
    parser.add_argument("--interval", type=_positive_float, default=0.25)
    parser.add_argument("--handles-per-tick", type=_positive_int, default=10)
    parser.add_argument(
        "--max-handles",
        type=_positive_int,
        default=800,
        help="Hard open-handle cap that keeps the fixture bounded.",
    )
    args = parser.parse_args()

    leaked: list[BinaryIO] = []
    deadline = time.monotonic() + args.duration

    with tempfile.TemporaryDirectory(prefix="draintracer-handles-") as temp_dir:
        path = Path(temp_dir) / "held-open.bin"
        path.touch()
        try:
            while time.monotonic() < deadline:
                to_open = min(
                    args.handles_per_tick,
                    args.max_handles - len(leaked),
                )
                for _ in range(max(0, to_open)):
                    leaked.append(path.open("a+b", buffering=0))

                print(f"handle_leak: holding {len(leaked)} files", flush=True)
                time.sleep(
                    min(args.interval, max(0.0, deadline - time.monotonic()))
                )
        finally:
            for handle in reversed(leaked):
                handle.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
