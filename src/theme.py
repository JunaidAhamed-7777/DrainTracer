"""Shared colors and encoding-safe glyph sets for the DrainTracer TUI."""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console


@dataclass(frozen=True)
class Palette:
    """Night-friendly accent palette; plain text keeps the terminal default."""

    accent: str = "#7aa2f7"
    accent_alt: str = "#bb9af7"
    cyan: str = "#7dcfff"
    teal: str = "#73daca"
    muted: str = "#737aa2"
    border: str = "#3b4261"
    ok: str = "#9ece6a"
    warn: str = "#e0af68"
    alert: str = "#ff9e64"
    danger: str = "#f7768e"
    critical: str = "#ff4f6d"
    surface: str = "#24283b"
    ink: str = "#1a1b26"


@dataclass(frozen=True)
class Glyphs:
    """Symbols used by the dashboard. ``spark`` runs from lowest to full."""

    spark: str
    brand: str
    dot: str
    up: str
    down: str
    flat: str
    ok: str
    warn: str
    error: str
    info: str
    meter_full: str
    meter_empty: str
    sep: str
    ellipsis: str
    tree_mid: str
    tree_last: str
    tree_pipe: str
    arrow: str


UNICODE_GLYPHS = Glyphs(
    spark="▁▂▃▄▅▆▇█",
    brand="◆",
    dot="●",
    up="▲",
    down="▼",
    flat="■",
    ok="✔",
    warn="▲",
    error="✖",
    info="•",
    meter_full="━",
    meter_empty="━",
    sep="·",
    ellipsis="…",
    tree_mid="├─ ",
    tree_last="└─ ",
    tree_pipe="│  ",
    arrow="→",
)

ASCII_GLYPHS = Glyphs(
    spark=".,:-=+*#",
    brand="*",
    dot="*",
    up="^",
    down="v",
    flat="=",
    ok="+",
    warn="!",
    error="x",
    info="-",
    meter_full="#",
    meter_empty="-",
    sep="|",
    ellipsis="...",
    tree_mid="|- ",
    tree_last="`- ",
    tree_pipe="|  ",
    arrow="->",
)

PALETTE = Palette()


def select_glyphs(console: Console) -> Glyphs:
    """Use Unicode symbols only when the console encoding can represent them."""
    sample = "".join(
        value for value in vars(UNICODE_GLYPHS).values() if isinstance(value, str)
    )
    # Rich falls back to ASCII boxes for non-UTF encodings; mirror that rule.
    encoding = (console.encoding or "utf-8").lower()
    if not encoding.startswith("utf"):
        return ASCII_GLYPHS
    try:
        sample.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return ASCII_GLYPHS
    return UNICODE_GLYPHS


def blend(start: str, end: str, amount: float) -> str:
    """Linearly blend two ``#rrggbb`` colors."""
    amount = min(max(amount, 0.0), 1.0)
    a = [int(start[index : index + 2], 16) for index in (1, 3, 5)]
    b = [int(end[index : index + 2], 16) for index in (1, 3, 5)]
    mixed = [round(x + (y - x) * amount) for x, y in zip(a, b)]
    return "#" + "".join(f"{channel:02x}" for channel in mixed)


__all__ = [
    "ASCII_GLYPHS",
    "Glyphs",
    "PALETTE",
    "Palette",
    "UNICODE_GLYPHS",
    "blend",
    "select_glyphs",
]
