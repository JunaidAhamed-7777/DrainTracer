"""Bounded, opt-in target log capture; raw output never enters a report."""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from pathlib import Path
from typing import BinaryIO

from .experiment import redact_text

MAX_LINE_BYTES = 8192


def _available(stream: BinaryIO) -> bytes | None:
    """A nonblocking chunk, b'' at EOF, or None when no data is ready."""
    descriptor = stream.fileno()
    if sys.platform == "win32":
        import msvcrt
        from ctypes import wintypes

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        peek = api.PeekNamedPipe
        peek.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                         ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        peek.restype = wintypes.BOOL
        count = wintypes.DWORD()
        if not peek(msvcrt.get_osfhandle(descriptor), None, 0, None, ctypes.byref(count), None):
            if ctypes.get_last_error() in (109, 232):  # broken/no-data pipe
                return b""
            raise OSError("Could not read captured output")
        if not count.value:
            return None
        return os.read(descriptor, min(count.value, 65536))
    os.set_blocking(descriptor, False)
    try:
        return os.read(descriptor, 65536)
    except BlockingIOError:
        return None


class BoundedLog:
    """Drain a pipe even after its file fills, without accumulating long lines.

    Redaction runs on complete UTF-8 lines before any bytes reach disk.
    Overlong lines are omitted entirely, rather than retaining secret fragments.
    """

    def __init__(self, stream: BinaryIO, path: Path, limit: int, secrets: tuple[str, ...]) -> None:
        self.stream = stream
        self.path = path
        self.limit = limit
        self.secrets = tuple(sorted({part for value in secrets for part in value.splitlines() if part},
                                    key=len, reverse=True))
        self.written = 0
        self.truncated = False
        self.omitted_lines = 0
        self.error = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._file: BinaryIO | None = None
        self._pending = bytearray()
        self._long_line = False

    def start(self) -> None:
        # Exclusive creation refuses symlinks and existing files; never overwrite.
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._file = os.fdopen(descriptor, "wb")
        self._thread = threading.Thread(target=self._drain, name="workload-log", daemon=True)
        self._thread.start()

    def _line(self, raw: bytes) -> None:
        text = raw.decode("utf-8", errors="replace")
        for secret in self.secrets:
            text = text.replace(secret, "<redacted>")
        data = redact_text(text).encode("utf-8")
        if self.written + len(data) > self.limit:
            self.truncated = True
            return
        if not self.truncated and self._file is not None:
            self._file.write(data)
            self.written += len(data)

    def _consume(self, chunk: bytes) -> None:
        for part in chunk.splitlines(keepends=True):
            ended = part.endswith((b"\n", b"\r"))
            if not self._long_line:
                if len(self._pending) + len(part) > MAX_LINE_BYTES:
                    self._pending.clear()
                    self._long_line = True
                    self.omitted_lines += 1
                    self.truncated = True
                else:
                    self._pending.extend(part)
            if ended:
                if not self._long_line:
                    self._line(bytes(self._pending))
                self._pending.clear()
                self._long_line = False

    def _drain(self) -> None:
        stop_at: float | None = None
        try:
            while True:
                if self._stop.is_set() and stop_at is None:
                    stop_at = time.monotonic() + 0.5
                if stop_at is not None and time.monotonic() >= stop_at:
                    self.truncated = True
                    break
                chunk = _available(self.stream)
                if chunk == b"":
                    break
                if chunk:
                    self._consume(chunk)
                elif stop_at is not None:
                    break
                else:
                    self._stop.wait(0.01)
            if self._pending and not self._long_line:
                self._line(bytes(self._pending))
        except (OSError, ValueError):
            self.error = True
        finally:
            self.stream.close()
            if self._file is not None:
                self._file.close()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2)
            if self._thread.is_alive():
                self.error = True
        else:
            self.stream.close()
            if self._file is not None:
                self._file.close()

    def record(self) -> dict:
        return {"path": str(self.path), "bytes": self.written, "limit_bytes": self.limit,
                "truncated": self.truncated, "omitted_lines": self.omitted_lines,
                "error": self.error, "redacted": True}
