"""Size-aware Rich renderables used by the dashboard."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from rich.cells import cell_len
from rich.console import Console, ConsoleOptions, RenderableType, RenderResult
from rich.segment import Segment
from rich.text import Text

from .theme import Glyphs


class Sparkline:
    """Multi-row block chart that fills the width it is given.

    The newest sample sits at the right edge. ``floor`` pins the lower bound
    (useful for percentages), and ``min_span`` keeps tiny noise on a large
    baseline from being stretched into full-height spikes.
    """

    def __init__(
        self,
        values: Sequence[float],
        glyphs: Glyphs,
        color: str,
        height: int = 2,
        floor: float | None = None,
        min_span: float = 0.0,
        empty_style: str = "dim",
    ) -> None:
        self.values = list(values)
        self.glyphs = glyphs
        self.color = color
        self.height = max(1, height)
        self.floor = floor
        self.min_span = max(0.0, min_span)
        self.empty_style = empty_style

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        width = max(1, options.max_width)
        points = self.values[-width:]
        if len(points) < 2:
            lines = [""] * (self.height - 1) + ["collecting samples"]
            yield Text("\n".join(lines), style=self.empty_style, no_wrap=True)
            return

        low = min(points) if self.floor is None else min(self.floor, min(points))
        high = max(points)
        if high - low < self.min_span:
            high = low + self.min_span

        ramp = self.glyphs.spark
        steps = len(ramp)
        levels = self.height * steps
        span = high - low
        scaled: list[int] = []
        for value in points:
            if span <= 0:
                scaled.append(1)
            else:
                fraction = (value - low) / span
                scaled.append(1 + round(fraction * (levels - 1)))

        padding = " " * (width - len(points))
        rows: list[str] = []
        for row in reversed(range(self.height)):
            cells: list[str] = []
            for level in scaled:
                fill = level - row * steps
                if fill >= steps:
                    cells.append(ramp[-1])
                elif fill > 0:
                    cells.append(ramp[fill - 1])
                else:
                    cells.append(" ")
            rows.append(padding + "".join(cells))
        yield Text("\n".join(rows), style=self.color, no_wrap=True, overflow="crop")


class LegacySafe:
    """Replace characters a legacy code page cannot show, keeping cell widths.

    The glyph sets already fall back to ASCII, but Rich adds its own "…"
    when it truncates text, and process names or paths may hold any
    character. On a non-UTF console each unencodable character becomes
    ``?`` repeated over its cell width (``.`` for the ellipsis), so the
    layout keeps its shape and nothing fails to print.
    """

    def __init__(self, renderable: RenderableType, encoding: str) -> None:
        self.renderable = renderable
        self.encoding = encoding

    @staticmethod
    def needed(encoding: str | None) -> bool:
        return not (encoding or "utf-8").lower().startswith("utf")

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        encoding = self.encoding
        cache: dict[str, str] = {}
        for segment in console.render(self.renderable, options):
            text = segment.text
            if not text or segment.control or text.isascii():
                yield segment
                continue
            try:
                text.encode(encoding)
            except UnicodeEncodeError:
                text = "".join(_legacy_char(char, encoding, cache) for char in text)
            except LookupError:
                text = "".join(_legacy_char(char, "ascii", cache) for char in text)
            yield Segment(text, segment.style, segment.control)


def _legacy_char(char: str, encoding: str, cache: dict[str, str]) -> str:
    known = cache.get(char)
    if known is not None:
        return known
    try:
        char.encode(encoding)
        replacement = char
    except UnicodeEncodeError:
        replacement = "." if char == "…" else "?" * max(1, cell_len(char))
    cache[char] = replacement
    return replacement


class Lines:
    """Pre-rendered lines of segments, for views that scroll their content."""

    def __init__(self, lines: Sequence[Sequence[Segment]]) -> None:
        self.lines = list(lines)

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        for line in self.lines:
            yield from line
            yield Segment.line()


class Sized:
    """Defer building a renderable until its final width and height are known."""

    def __init__(self, factory: Callable[[int, int | None], RenderableType]) -> None:
        self.factory = factory

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        yield self.factory(options.max_width, options.height)


__all__ = ["LegacySafe", "Lines", "Sized", "Sparkline"]
