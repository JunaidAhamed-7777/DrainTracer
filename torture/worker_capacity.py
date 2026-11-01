"""Bounded 12 MiB worker accumulation or churn for attribution measurements."""

from __future__ import annotations

import argparse
import math
import subprocess
import sys
import time


def duration(value):
    seconds = float(value)
    if not math.isfinite(seconds) or not 0 < seconds <= 120:
        raise argparse.ArgumentTypeError("duration must be finite, positive, and at most 120 seconds")
    return seconds


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("accumulate", "churn"))
    parser.add_argument("--duration", type=duration, default=90)
    args = parser.parse_args(argv)
    deadline = time.monotonic() + args.duration
    next_spawn = time.monotonic()
    children = []
    started = 0
    cap = 8 if args.mode == "accumulate" else 64
    try:
        while time.monotonic() < deadline:
            retained = []
            for child in children:
                if child.poll() is None:
                    retained.append(child)
                else:
                    child.wait()
            children = retained
            now = time.monotonic()
            if now >= next_spawn and started < cap and len(children) < 8:
                lifetime = deadline - now if args.mode == "accumulate" else min(0.75, deadline - now)
                code = ("import time; memory=bytearray(12*1024*1024); "
                        "memory[::4096]=b'x'*(len(memory)//4096); "
                        f"time.sleep({lifetime!r})")
                children.append(subprocess.Popen([sys.executable, "-c", code],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                started += 1
                next_spawn = now + 2
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        cleanup_deadline = time.monotonic() + 2
        for child in children:
            try:
                child.wait(timeout=max(0.01, cleanup_deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
