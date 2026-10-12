"""Exit the root after spawning a bounded child that remains alive."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--parent-delay",
        type=_positive_float,
        default=1.5,
        help="Seconds the root stays alive so DrainTracer can discover its child.",
    )
    parser.add_argument(
        "--child-duration",
        type=_positive_float,
        default=12.0,
        help="Bounded child lifetime if the monitor does not terminate it.",
    )
    args = parser.parse_args()

    child_code = (
        "import time; "
        "memory = bytearray(4 * 1024 * 1024); "
        "memory[::4096] = b'x' * (len(memory) // 4096); "
        f"time.sleep({args.child_duration!r})"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"parent_exits: spawned child PID {child.pid}", flush=True)
    time.sleep(args.parent_delay)
    print("parent_exits: root exiting while child remains alive", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
