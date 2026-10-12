"""Grow committed private memory monotonically, then release it on exit."""

from __future__ import annotations

import argparse
import time


MIB = 1024 * 1024


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def _touch_pages(block: bytearray) -> None:
    for offset in range(0, len(block), 4096):
        block[offset] = 0xA5


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=_positive_float, default=20.0)
    parser.add_argument("--interval", type=_positive_float, default=0.25)
    parser.add_argument("--chunk-mb", type=_positive_float, default=2.0)
    parser.add_argument(
        "--max-mb",
        type=_positive_float,
        default=128.0,
        help="Hard allocation cap that keeps the fixture bounded.",
    )
    args = parser.parse_args()

    chunk_bytes = max(1, int(args.chunk_mb * MIB))
    max_bytes = max(1, int(args.max_mb * MIB))
    leaked: list[bytearray] = []
    allocated = 0
    deadline = time.monotonic() + args.duration

    while time.monotonic() < deadline:
        remaining = max_bytes - allocated
        if remaining > 0:
            block = bytearray(min(chunk_bytes, remaining))
            _touch_pages(block)
            leaked.append(block)
            allocated += len(block)

        print(
            f"memory_leak: holding {allocated / MIB:.1f} MiB "
            f"in {len(leaked)} blocks",
            flush=True,
        )
        time.sleep(min(args.interval, max(0.0, deadline - time.monotonic())))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
