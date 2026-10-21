"""Non-blocking key input decoded into symbolic names for the dashboard.

Terminals deliver navigation keys as multi-character sequences: the
Windows console prefixes them with ``\\x00`` or ``\\xe0`` followed by a scan
code, and POSIX terminals send ANSI escape sequences such as ``\\x1b[A``.
Reading them one character at a time would turn an arrow key into a stray
letter (``H`` is the help key), so both forms are decoded here into names
like ``up`` and ``pgdn``. Printable keys are returned unchanged as single
characters, so a name is always longer than one character.
"""

from __future__ import annotations

import os
import sys
from collections import deque
from collections.abc import Callable

UP = "up"
DOWN = "down"
LEFT = "left"
RIGHT = "right"
HOME = "home"
END = "end"
PAGE_UP = "pgup"
PAGE_DOWN = "pgdn"
ENTER = "enter"
TAB = "tab"
BACKTAB = "backtab"
ESCAPE = "escape"
BACKSPACE = "backspace"
DELETE = "delete"

NAMED_KEYS = frozenset(
    (UP, DOWN, LEFT, RIGHT, HOME, END, PAGE_UP, PAGE_DOWN, ENTER, TAB, BACKTAB, ESCAPE,
     BACKSPACE, DELETE)
)

# Scan codes that follow the Windows console's \x00 / \xe0 prefix.
WINDOWS_SCAN_CODES = {
    "H": UP,
    "P": DOWN,
    "K": LEFT,
    "M": RIGHT,
    "G": HOME,
    "O": END,
    "I": PAGE_UP,
    "Q": PAGE_DOWN,
    "S": DELETE,
    "\x0f": BACKTAB,
}
_WINDOWS_PREFIXES = ("\x00", "\xe0")

# Final characters of CSI (``\x1b[``) and SS3 (``\x1bO``) cursor sequences.
_ANSI_FINALS = {
    "A": UP,
    "B": DOWN,
    "C": RIGHT,
    "D": LEFT,
    "H": HOME,
    "F": END,
    "Z": BACKTAB,
}
# ``\x1b[N~`` sequences by their number.
_ANSI_TILDE = {
    "1": HOME,
    "7": HOME,
    "4": END,
    "8": END,
    "3": DELETE,
    "5": PAGE_UP,
    "6": PAGE_DOWN,
}


def decode_plain(character: str) -> str | None:
    """Name a single non-prefixed character, or return it when printable."""
    if character in ("\r", "\n"):
        return ENTER
    if character == "\t":
        return TAB
    if character == "\x1b":
        return ESCAPE
    if character in ("\x08", "\x7f"):
        return BACKSPACE
    if character.isprintable():
        return character
    return None


def decode_windows(first: str, read_next: Callable[[], str | None]) -> str | None:
    """Decode one Windows console key; ``read_next`` supplies the scan code.

    Unknown extended keys (function keys, Insert) return ``None`` after
    their scan code is consumed, so it never surfaces as a letter.
    """
    if first in _WINDOWS_PREFIXES:
        code = read_next()
        if not code:
            return None
        return WINDOWS_SCAN_CODES.get(code)
    return decode_plain(first)


def decode_ansi(data: str) -> list[str]:
    """Decode a chunk of POSIX terminal input into key names and characters.

    A chunk holds whatever the terminal delivered at once, so an escape
    sequence arrives whole; a lone ``\\x1b`` (or one followed by another
    escape) is the Escape key. Unknown sequences are skipped entirely.
    Modifier parameters (``\\x1b[1;5A``) are ignored: Ctrl+Up is still Up.
    """
    keys: list[str] = []
    index, length = 0, len(data)
    while index < length:
        character = data[index]
        if character != "\x1b":
            decoded = decode_plain(character)
            if decoded is not None:
                keys.append(decoded)
            index += 1
            continue
        introducer = data[index + 1] if index + 1 < length else ""
        if introducer not in ("[", "O"):
            keys.append(ESCAPE)
            index += 1
            continue
        # Parameters are digits and separators; the final byte names the key.
        end = index + 2
        while end < length and data[end] in "0123456789;":
            end += 1
        if end >= length:
            break  # a truncated sequence: drop it rather than type its tail
        final, params = data[end], data[index + 2 : end]
        if final == "~":
            name = _ANSI_TILDE.get(params.split(";", 1)[0])
        else:
            name = _ANSI_FINALS.get(final)
        if name is not None:
            keys.append(name)
        index = end + 1
    return keys


class KeyReader:
    """Return at most one decoded key per call without blocking.

    POSIX reads take whatever bytes are waiting, which can hold several
    keys; the extras queue up for later calls.
    """

    def __init__(self) -> None:
        self._pending: deque[str] = deque()

    def read(self) -> str | None:
        if self._pending:
            return self._pending.popleft()
        if os.name == "nt":
            return self._read_windows()
        self._pending.extend(self._read_posix())
        return self._pending.popleft() if self._pending else None

    @staticmethod
    def _read_windows() -> str | None:
        import msvcrt

        if not msvcrt.kbhit():
            return None

        def scan_code() -> str | None:
            # An extended key's scan code is already buffered. "\xe0" is also
            # "à" on some layouts, with nothing after it: a blocking read
            # would freeze the dashboard until the next key press.
            return msvcrt.getwch() if msvcrt.kbhit() else None

        return decode_windows(msvcrt.getwch(), scan_code)

    @staticmethod
    def _read_posix() -> list[str]:
        import select

        stream = sys.stdin
        try:
            if stream is None or not stream.isatty():
                return []
            ready, _, _ = select.select([stream], [], [], 0)
            if not ready:
                return []
            data = os.read(stream.fileno(), 64)
        except (OSError, ValueError):
            return []
        return decode_ansi(data.decode("utf-8", errors="ignore"))


def normalize_key(key: str) -> str:
    """Name raw control characters so handlers compare names only."""
    if len(key) == 1:
        return decode_plain(key) or key
    return key


__all__ = [
    "BACKSPACE",
    "BACKTAB",
    "DELETE",
    "DOWN",
    "END",
    "ENTER",
    "ESCAPE",
    "HOME",
    "KeyReader",
    "LEFT",
    "NAMED_KEYS",
    "PAGE_DOWN",
    "PAGE_UP",
    "RIGHT",
    "TAB",
    "UP",
    "WINDOWS_SCAN_CODES",
    "decode_ansi",
    "decode_plain",
    "decode_windows",
    "normalize_key",
]
