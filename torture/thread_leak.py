"""Add blocked worker threads monotonically while keeping a strict cap."""

from __future__ import annotations

import argparse
import threading
import time


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


def _wait_forever(stop: threading.Event) -> None:
    stop.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=_positive_float, default=20.0)
    parser.add_argument("--interval", type=_positive_float, default=0.25)
    parser.add_argument("--threads-per-tick", type=_positive_int, default=1)
    parser.add_argument(
        "--max-threads",
        type=_positive_int,
        default=80,
        help="Hard worker cap that keeps the fixture bounded.",
    )
    args = parser.parse_args()

    stop = threading.Event()
    leaked: list[threading.Thread] = []
    deadline = time.monotonic() + args.duration

    try:
        while time.monotonic() < deadline:
            to_start = min(
                args.threads_per_tick,
                args.max_threads - len(leaked),
            )
            for _ in range(max(0, to_start)):
                worker = threading.Thread(
                    target=_wait_forever,
                    args=(stop,),
                    name=f"torture-worker-{len(leaked) + 1}",
                    daemon=True,
                )
                worker.start()
                leaked.append(worker)

            print(f"thread_leak: holding {len(leaked)} workers", flush=True)
            time.sleep(min(args.interval, max(0.0, deadline - time.monotonic())))
    finally:
        stop.set()
        for worker in leaked:
            worker.join(timeout=1.0)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
