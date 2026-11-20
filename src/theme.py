"""Shared colors and encoding-safe glyph sets for the DrainTracer TUI."""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console


def plain_console(console: Console) -> Console:
    """Reuse the output destination while disabling styles and terminal control."""
    return Console(file=console.file, width=console.width, color_system=None,
                   force_terminal=False, no_color=True, markup=False,
                   highlight=False, emoji=False)


@dataclass(frozen=True)
class Palette:
    """Brand blue and teal, with violet for secondary data and warm signal colors."""

    accent: str = "#53b7ff"
    accent_alt: str = "#c29aff"
    cyan: str = "#43e0ff"
    teal: str = "#73daca"
    muted: str = "#adbccc"
    border: str = "#647d95"
    ok: str = "#82e78a"
    warn: str = "#ffd04e"
    alert: str = "#ffa34d"
    danger: str = "#ff8076"
    critical: str = "#ff6995"
    surface: str = "#203449"
    ink: str = "#101820"


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
    meter_empty="·",
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
    "PALETTE",
    "UNICODE_GLYPHS",
    "Glyphs",
    "Palette",
    "blend",
    "plain_console",
    "select_glyphs",
]
