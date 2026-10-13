"""Open loopback TCP connections monotonically while keeping a strict cap."""

from __future__ import annotations

import argparse
import socket
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=_positive_float, default=20.0)
    parser.add_argument("--interval", type=_positive_float, default=0.25)
    parser.add_argument("--connections-per-tick", type=_positive_int, default=5)
    parser.add_argument(
        "--max-connections",
        type=_positive_int,
        default=300,
        help="Hard connection cap; each connection holds two sockets.",
    )
    args = parser.parse_args()

    # Loopback only: nothing leaves the machine and no firewall prompt appears.
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(128)
    server.settimeout(5.0)
    leaked: list[socket.socket] = []
    deadline = time.monotonic() + args.duration
    try:
        while time.monotonic() < deadline:
            to_open = min(args.connections_per_tick, args.max_connections - len(leaked) // 2)
            for _ in range(max(0, to_open)):
                client = socket.create_connection(server.getsockname(), timeout=5.0)
                accepted, _address = server.accept()
                leaked.extend((client, accepted))

            print(f"socket_leak: holding {len(leaked) // 2} connections", flush=True)
            time.sleep(min(args.interval, max(0.0, deadline - time.monotonic())))
    finally:
        for connection in reversed(leaked):
            connection.close()
        server.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
