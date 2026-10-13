"""Size-aware Rich renderables used by the dashboard."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from rich.console import Console, ConsoleOptions, RenderableType, RenderResult
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


class Sized:
    """Defer building a renderable until its final width and height are known."""

    def __init__(self, factory: Callable[[int, int | None], RenderableType]) -> None:
        self.factory = factory

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        yield self.factory(options.max_width, options.height)


__all__ = ["Sized", "Sparkline"]
