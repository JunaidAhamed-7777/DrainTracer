"""Immediate keyboard input for terminal startup and process pickers."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from rich.console import Console

from .keys import KeyReader


@dataclass(frozen=True)
class PickerKey:
    """Keep navigation events distinct from typed process names or PIDs."""

    kind: Literal["up", "down", "enter", "backspace", "escape", "text"]
    text: str = ""


def supports_picker_keyboard(console: Console) -> bool:
    """Use native key events only when both input and output are terminals."""
    if not console.is_terminal or sys.stdin is None:
        return False
    try:
        return sys.stdin.isatty()
    except (OSError, ValueError):
        return False


class PickerKeyboard:
    """Read no-echo keys; use as a context to restore POSIX terminal input."""

    def __init__(self, getwch: Callable[[], str] | None = None) -> None:
        self._reader = KeyReader() if getwch is None and os.name != "nt" else None
        if getwch is None and self._reader is None:
            import msvcrt

            getwch = msvcrt.getwch
        self._getwch = getwch
        # input() precedes this picker; a stray LF must not select a program.
        self._skip_lf = True

    def __enter__(self) -> PickerKeyboard:
        if self._reader is not None:
            self._reader.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._reader is not None:
            self._reader.__exit__(*exc)

    def _read_posix(self) -> PickerKey:
        import select

        while True:
            key = self._reader.read()
            if key == "\x03":
                raise KeyboardInterrupt
            if key in ("\x04", "\x1a") or self._reader.eof:
                raise EOFError
            if key in ("up", "down", "enter", "backspace", "escape"):
                return PickerKey(key)
            if key is not None and len(key) == 1 and key.isprintable():
                return PickerKey("text", key)
            if key is None:
                # A short timeout also resolves a lone Escape, while keeping
                # partial escape and UTF-8 sequences buffered between reads.
                try:
                    select.select([sys.stdin], [], [], 0.05)
                except (OSError, ValueError) as exc:
                    raise EOFError from exc

    def read(self) -> PickerKey:
        """Block for a usable key, consuming complete extended-key sequences."""
        if self._reader is not None:
            return self._read_posix()
        while True:
            character = self._getwch()
            if not character or character in ("\x04", "\x1a"):
                raise EOFError
            if character == "\x03":
                raise KeyboardInterrupt
            if self._skip_lf and character == "\n":
                continue
            self._skip_lf = character == "\r"
            if character in ("\x00", "\xe0"):
                scan_code = self._getwch()
                if not scan_code:
                    raise EOFError
                if scan_code in ("H", "P"):
                    return PickerKey("up" if scan_code == "H" else "down")
            elif character in ("\r", "\n"):
                return PickerKey("enter")
            elif character in ("\x08", "\x7f"):
                return PickerKey("backspace")
            elif character == "\x1b":
                return PickerKey("escape")
            elif character.isprintable():
                return PickerKey("text", character)
