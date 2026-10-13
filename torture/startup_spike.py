"""Allocate memory quickly while "starting up", then hold it steady."""

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
        block[offset] = 0x5A


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=_positive_float, default=20.0)
    parser.add_argument(
        "--startup-seconds",
        type=_positive_float,
        default=2.5,
        help="How long the startup allocation ramp lasts.",
    )
    parser.add_argument(
        "--startup-mb",
        type=_positive_float,
        default=96.0,
        help="Memory allocated during startup; a hard cap that keeps the fixture bounded.",
    )
    args = parser.parse_args()

    step = 0.1
    steps = max(1, round(args.startup_seconds / step))
    chunk = max(1, int(args.startup_mb * MIB / steps))
    held: list[bytearray] = []
    deadline = time.monotonic() + args.duration

    # Startup: caches, assets, and JIT state arrive quickly...
    for _ in range(steps):
        if time.monotonic() >= deadline:
            break
        block = bytearray(chunk)
        _touch_pages(block)
        held.append(block)
        time.sleep(step)
    print(f"startup_spike: warmed up with {len(held) * chunk / MIB:.0f} MiB", flush=True)

    # ...and then the steady state holds them without growing.
    while time.monotonic() < deadline:
        print(f"startup_spike: steady at {len(held) * chunk / MIB:.0f} MiB", flush=True)
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
