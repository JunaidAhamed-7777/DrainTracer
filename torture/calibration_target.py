"""Hard-bounded cache, allocation/recovery, and short-worker calibration inputs."""

from __future__ import annotations

import argparse
import gc
import math
import subprocess
import sys
import time

MIB = 1024 * 1024


def _duration(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or not 0 < result <= 120:
        raise argparse.ArgumentTypeError("duration must be finite and between zero and 120 seconds")
    return result


def _touch(size: int) -> bytearray:
    block = bytearray(size)
    for offset in range(0, len(block), 4096):
        block[offset] = 1
    return block


def run(mode: str, duration: float) -> None:
    deadline = time.monotonic() + duration
    children: list[subprocess.Popen] = []
    held: list[bytearray] = []

    def pause(seconds: float) -> None:
        time.sleep(min(seconds, max(0, deadline - time.monotonic())))

    try:
        while time.monotonic() < deadline:
            if mode == "cache":
                if len(held) < 24:
                    held.append(_touch(MIB))
                pause(0.25)
            elif mode == "recovery":
                held.append(_touch(32 * MIB))
                pause(1)
                held.clear()
                gc.collect()
                pause(1)
            else:
                children = [child for child in children if child.poll() is None]
                if len(children) < 4:
                    children.append(subprocess.Popen(
                        [sys.executable, "-c", "import time; time.sleep(0.75)"],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    ))
                pause(0.5)
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=2)
        # Keep cache allocations alive through the end of the measurement.
        if held:
            assert len(held) <= 24


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("cache", "recovery", "churn"))
    parser.add_argument("--duration", type=_duration, default=90.0)
    args = parser.parse_args(argv)
    run(args.mode, args.duration)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
