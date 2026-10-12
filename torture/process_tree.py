"""Build a bounded recursive process tree with stable per-process resources."""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path


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


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be zero or greater")
    return parsed


def _hold_thread(stop: threading.Event) -> None:
    stop.wait()


def _stop_children(children: list[subprocess.Popen[bytes]]) -> None:
    for child in children:
        try:
            child.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            child.terminate()

    for child in children:
        if child.poll() is not None:
            continue
        try:
            child.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=1.0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=_positive_float, default=20.0)
    parser.add_argument("--depth", type=_non_negative_int, default=2)
    parser.add_argument("--fanout", type=_positive_int, default=2)
    parser.add_argument("--memory-mb", type=_non_negative_int, default=2)
    args = parser.parse_args()

    memory = [bytearray(1024 * 1024) for _ in range(args.memory_mb)]
    for block in memory:
        for offset in range(0, len(block), 4096):
            block[offset] = 1

    stop = threading.Event()
    worker = threading.Thread(target=_hold_thread, args=(stop,), daemon=True)
    worker.start()

    children: list[subprocess.Popen[bytes]] = []
    if args.depth > 0:
        child_duration = max(0.2, args.duration - 1.0)
        script = str(Path(__file__).resolve())
        for _ in range(args.fanout):
            children.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        script,
                        "--duration",
                        str(child_duration),
                        "--depth",
                        str(args.depth - 1),
                        "--fanout",
                        str(args.fanout),
                        "--memory-mb",
                        str(args.memory_mb),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )

    try:
        deadline = time.monotonic() + args.duration
        while time.monotonic() < deadline:
            live_children = sum(child.poll() is None for child in children)
            print(
                f"process_tree: depth={args.depth}, "
                f"live_children={live_children}, memory={len(memory)} MiB",
                flush=True,
            )
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
    finally:
        _stop_children(children)
        stop.set()
        worker.join(timeout=1.0)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
