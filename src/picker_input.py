"""Immediate keyboard input for the Windows running-program picker."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from rich.console import Console


@dataclass(frozen=True)
class PickerKey:
    """Keep navigation events distinct from typed process names or PIDs."""

    kind: Literal["up", "down", "enter", "backspace", "escape", "text"]
    text: str = ""


def supports_picker_keyboard(console: Console) -> bool:
    """Use native key events only when both input and output are terminals."""
    if os.name != "nt" or not console.is_terminal or sys.stdin is None:
        return False
    try:
        return sys.stdin.isatty()
    except (OSError, ValueError):
        return False


class PickerKeyboard:
    """Read native no-echo keys without changing the terminal's input mode."""

    def __init__(self, getwch: Callable[[], str] | None = None) -> None:
        if getwch is None:
            import msvcrt

            getwch = msvcrt.getwch
        self._getwch = getwch
        # input() precedes this picker; a stray LF must not select a program.
        self._skip_lf = True

    def read(self) -> PickerKey:
        """Block for a usable key, consuming complete extended-key sequences."""
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
