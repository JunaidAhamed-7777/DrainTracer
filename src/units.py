"""Parse and format the human-friendly durations used by flags and reports."""

from __future__ import annotations

import math
import re

_SECONDS_PER_UNIT = {
    "ms": 0.001,
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "m": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hrs": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
}
_DURATION_PART = re.compile(r"\s*(\d+(?:\.\d*)?|\.\d+)\s*([a-z]+)?\s*")
_DURATION_HINT = "a duration such as 45, 45s, 2m, 1m30s, 1.5h, or 250ms"


def parse_duration(value: str | int | float) -> float:
    """Return the number of seconds in ``value``.

    Accepts a bare number of seconds or one or more ``<number><unit>``
    parts (``ms``, ``s``, ``m``, ``h`` and their long forms), for example
    ``"90"``, ``"90s"``, ``"1m30s"``, or ``"250ms"``. Raises ``ValueError``
    for anything else, including negative and non-finite values.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"expected {_DURATION_HINT}, got {value!r}")
    if isinstance(value, str):
        seconds = _parse_duration_text(value)
    else:
        seconds = float(value)
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f"expected {_DURATION_HINT}, got {value!r}")
    return seconds


def _parse_duration_text(value: str) -> float:
    text = value.strip().lower()
    parts: list[tuple[str, str | None]] = []
    position = 0
    while position < len(text):
        match = _DURATION_PART.match(text, position)
        if match is None or match.end() == position:
            raise ValueError(f"expected {_DURATION_HINT}, got {value!r}")
        parts.append((match.group(1), match.group(2)))
        position = match.end()
    if not parts:
        raise ValueError(f"expected {_DURATION_HINT}, got {value!r}")
    if len(parts) > 1 and any(unit is None for _number, unit in parts):
        raise ValueError(f"every part of {value!r} needs a unit, as in 1m30s")

    seconds = 0.0
    for number, unit in parts:
        scale = _SECONDS_PER_UNIT.get(unit or "s")
        if scale is None:
            raise ValueError(f"unknown duration unit {unit!r} in {value!r}")
        seconds += float(number) * scale
    return seconds


def format_duration(seconds: float) -> str:
    """Render seconds compactly: ``250ms``, ``45s``, ``1m30s``, ``2h5m``."""
    if not math.isfinite(seconds) or seconds <= 0:
        return "0s"
    millis = round(seconds * 1000)
    if millis < 1000:
        return f"{millis}ms"
    tenths = round(seconds, 1)
    if tenths < 60:
        return f"{tenths:g}s"
    whole = round(seconds)
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = [(hours, "h"), (minutes, "m"), (secs, "s")]
    return "".join(f"{amount}{unit}" for amount, unit in parts if amount)


__all__ = ["format_duration", "parse_duration"]
