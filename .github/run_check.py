"""Stream a CI command and retain at most 256 KiB of its diagnostic output."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

LOG_LIMIT = 256 * 1024


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=(
        Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())) / "draintracer-checks"
    ))
    parser.add_argument("name")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[a-z0-9-]{1,48}", args.name):
        parser.error("name must contain 1-48 lowercase letters, digits, or hyphens")
    if not args.command:
        parser.error("a command is required")
    args.directory.mkdir(parents=True, exist_ok=True)
    tail = bytearray()
    discarded = 0
    try:
        with subprocess.Popen(args.command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as child:
            try:
                while chunk := child.stdout.read(8192):
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                    tail.extend(chunk)
                    if len(tail) > LOG_LIMIT:
                        excess = len(tail) - LOG_LIMIT
                        del tail[:excess]
                        discarded += excess
                result = child.wait()
            except BaseException:
                child.kill()
                child.wait()
                raise
    except OSError as error:
        tail.extend(str(error).encode("utf-8", errors="replace"))
        result = 2
    finally:
        prefix = f"[earlier diagnostic bytes omitted: {discarded}]\n".encode() if discarded else b""
        (args.directory / f"{args.name}.log").write_bytes(prefix + tail[-LOG_LIMIT:])
    return result if result >= 0 else 128 - result


if __name__ == "__main__":
    raise SystemExit(main())
