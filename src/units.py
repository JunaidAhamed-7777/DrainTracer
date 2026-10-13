"""Parse and format the durations and sizes used by flags, the UI, and reports."""

from __future__ import annotations

import math
import re
from collections.abc import Callable

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


_BYTES_PER_UNIT = {
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "gib": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
    "tib": 1024**4,
}
_AMOUNT = re.compile(r"\s*(\d+(?:\.\d*)?|\.\d+)\s*([a-z]*)\s*")
_SECONDS_PER_PERIOD = {
    "s": 1.0,
    "sec": 1.0,
    "second": 1.0,
    "min": 60.0,
    "minute": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hour": 3600.0,
}


def parse_size(value: str | int | float) -> float:
    """Return the number of bytes in ``value``.

    Accepts a bare number of bytes or ``<number><unit>`` such as ``"50MB"``
    or ``"1.5 GiB"``. Units are binary, as everywhere in DrainTracer:
    ``KB`` and ``KiB`` both mean 1,024 bytes.
    """
    hint = "a size such as 512KB, 50MB, or 1.5GB"
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"expected {hint}, got {value!r}")
    if isinstance(value, str):
        match = _AMOUNT.fullmatch(value.lower())
        if match is None:
            raise ValueError(f"expected {hint}, got {value!r}")
        scale = _BYTES_PER_UNIT.get(match.group(2) or "b")
        if scale is None:
            raise ValueError(f"unknown size unit {match.group(2)!r} in {value!r}")
        size = float(match.group(1)) * scale
    else:
        size = float(value)
    if not math.isfinite(size) or size < 0:
        raise ValueError(f"expected {hint}, got {value!r}")
    return size


def parse_count(value: str | int | float) -> float:
    """Return a non-negative count from a number or a numeric string."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"expected a number, got {value!r}")
    try:
        count = float(value)
    except ValueError:
        raise ValueError(f"expected a number, got {value!r}") from None
    if not math.isfinite(count) or count < 0:
        raise ValueError(f"expected a non-negative number, got {value!r}")
    return count


def parse_rate(value: str | int | float, amount: Callable[[str | int | float], float]) -> float:
    """Return a per-second rate such as ``"5MB/s"``, ``"300MB/min"``, or ``"20/s"``.

    ``amount`` parses the part before the slash (``parse_size`` or
    ``parse_count``); without a slash the amount is per second.
    """
    if isinstance(value, str) and "/" in value:
        quantity, _, period = value.rpartition("/")
        seconds = _SECONDS_PER_PERIOD.get(period.strip().lower())
        if seconds is None:
            raise ValueError(f"unknown rate period {period.strip()!r} in {value!r}; use /s, /min, or /h")
        return amount(quantity) / seconds
    return amount(value)


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


def format_clock(seconds: float) -> str:
    """Render elapsed seconds as ``HH:MM:SS``, prefixed by days when needed."""
    whole = int(max(0.0, seconds)) if math.isfinite(seconds) else 0
    days, remainder = divmod(whole, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    clock = f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{days}d {clock}" if days else clock


def format_bytes(value: float) -> str:
    """Render a byte count in binary units: ``512.0 B``, ``1.5 MB``.

    Negative and non-finite values render as zero; callers add signs.
    """
    units = ("B", "KB", "MB", "GB", "TB")
    amount = max(0.0, value) if math.isfinite(value) else 0.0
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:,.1f} {unit}"
        amount /= 1024
    return f"{amount:,.1f} TB"


__all__ = [
    "format_bytes",
    "format_clock",
    "format_duration",
    "parse_count",
    "parse_duration",
    "parse_rate",
    "parse_size",
]
