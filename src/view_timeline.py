"""View 4: the expanded timeline, or "when did growth start, and what happened around it?".

One resource chart at a time over the dashboard's recent history, with
annotations under it for warm-up end, process starts, exits and
restarts, pauses, unreadable processes, signal changes, the window
start of an active signal, and user markers (M). Columns are buckets of
time: each shows the highest sample in its bucket, a pause leaves a gap,
and the caption says how many samples each column stands for, so a
coarse chart never pretends to hold every sample.

F freezes this view only: sampling, detection and the target carry on
(Space still pauses the target), and the view says how many samples
arrived since it was frozen.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass

from rich import box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .events import (
    RESOURCE_METRICS,
    TRACKED_METRICS,
    LeakAlert,
    TimelineKind,
    TimelineMark,
)
from .keys import BACKTAB, DOWN, END, HOME, LEFT, PAGE_DOWN, PAGE_UP, RIGHT, TAB, UP
from .units import format_bytes, format_duration
from .widgets import Sized

# Charts cycled with Left/Right: the signal metrics first, then context.
TIMELINE_METRICS = ("private_bytes", "handles", "threads", "rss_bytes", "cpu_percent")
_TITLES = {
    "private_bytes": "PRIVATE BYTES",
    "handles": "HANDLES",
    "threads": "THREADS",
    "rss_bytes": "RSS (WORKING SET)",
    "cpu_percent": "CPU",
}
# One ASCII character per annotation kind, so legacy consoles show them too.
MARK_SYMBOLS = {
    TimelineKind.WARMUP_END: "W",
    TimelineKind.STARTED: "+",
    TimelineKind.EXITED: "x",
    TimelineKind.RESTARTED: "r",
    TimelineKind.PAUSED: "P",
    TimelineKind.RESUMED: ">",
    TimelineKind.SIGNAL: "!",
    TimelineKind.CLEARED: "c",
    TimelineKind.ACCESS_LOST: "?",
    TimelineKind.ACCESS_REGAINED: "a",
}
_LEGEND = {
    "W": "warm-up end",
    "+": "start",
    "x": "exit",
    "r": "restart",
    "P": "pause",
    ">": "resume",
    "!": "signal",
    "c": "cleared",
    "?": "unreadable",
    "a": "readable",
    "[": "signal window start",
    "*": "several",
}
# A gap between samples longer than this many intervals is drawn as a gap.
GAP_INTERVALS = 3.0
Y_LABEL_WIDTH = 9


@dataclass(frozen=True)
class FrozenTimeline:
    """What the timeline showed when F was pressed; sampling continues meanwhile."""

    frozen_at: float
    history: dict[str, tuple[tuple[float, float], ...]]
    marks: tuple[TimelineMark, ...]
    alerts: tuple[LeakAlert, ...]
    sample_count: int
    scope: str = "tree"


def mark_symbol(mark: TimelineMark) -> str:
    if mark.kind == TimelineKind.MARKER:
        return str(mark.index) if mark.index is not None and mark.index < 10 else "M"
    return MARK_SYMBOLS.get(mark.kind, "*")


def bucket_columns(
    points: Sequence[tuple[float, float]],
    start: float,
    end: float,
    columns: int,
    gap_seconds: float,
) -> tuple[list[float | None], float]:
    """Highest value per time bucket, and the mean samples per filled column.

    Columns without a sample repeat the previous value when the samples
    around them are close together (the chart is just wider than the
    data), and stay empty across a longer gap such as a pause.
    """
    values: list[float | None] = [None] * columns
    if not points or columns <= 0:
        return values, 0.0
    span = max(end - start, 1e-9)
    owner: list[float | None] = [None] * columns  # last sample time per column
    for stamp, value in points:
        index = min(columns - 1, max(0, int((stamp - start) / span * columns)))
        current = values[index]
        values[index] = value if current is None else max(current, value)
        owner[index] = stamp
    filled = sum(1 for value in values if value is not None)
    previous: tuple[float, float] | None = None
    for index in range(columns):
        if values[index] is not None:
            previous = (owner[index] or 0.0, values[index])  # type: ignore[assignment]
            continue
        if previous is None:
            continue
        upcoming = next(
            ((owner[later] or 0.0) for later in range(index + 1, columns) if values[later] is not None),
            None,
        )
        if upcoming is not None and upcoming - previous[0] <= gap_seconds:
            values[index] = previous[1]
    return values, (len(points) / filled if filled else 0.0)


def mark_row(
    marks: Sequence[TimelineMark], start: float, end: float, columns: int,
    window_start: float | None = None,
) -> str:
    """One character per column: the annotation symbol at its time, ``*`` for several."""
    row = [" "] * columns
    span = max(end - start, 1e-9)

    def place(stamp: float, symbol: str) -> None:
        if stamp < start or stamp > end or columns <= 0:
            return
        index = min(columns - 1, max(0, int((stamp - start) / span * columns)))
        row[index] = symbol if row[index] == " " else "*"

    if window_start is not None:
        place(window_start, "[")
    for mark in marks:
        place(mark.timestamp, mark_symbol(mark))
    return "".join(row)


class TimelineViewMixin:
    """Render and drive the timeline view for ``DashboardApp``."""

    def _init_timeline_view(self) -> None:
        self._timeline_metric = 0
        self._timeline_offset = 0
        self._timeline_frozen: FrozenTimeline | None = None
        self._timeline_identity: tuple[int, float | None] | None = None
        self._timeline_scope = "tree"
        self._timeline_range: float | None = None
        self._timeline_pan = 0.0

    @property
    def timeline_frozen(self) -> bool:
        return self._timeline_frozen is not None

    @property
    def timeline_metric(self) -> str:
        return TIMELINE_METRICS[self._timeline_metric]

    def _timeline_key(self, key: str) -> bool:
        if key in (LEFT, RIGHT, TAB, BACKTAB):
            step = -1 if key in (LEFT, BACKTAB) else 1
            metrics = TRACKED_METRICS if self._timeline_identity is not None else TIMELINE_METRICS
            self._timeline_metric = (self._timeline_metric + step) % len(metrics)
            return True
        if key.lower() == "p":
            if self.timeline_frozen:
                self._append_event("Release the frozen timeline before changing process scope", "info")
            elif self._timeline_identity is not None:
                self._timeline_identity, self._timeline_scope = None, "tree"
            else:
                row = self._selected_row(self._inspector_rows())
                if row is not None and row.identity[1] is not None:
                    self._timeline_identity = row.identity
                    self._timeline_scope = f"PID {row.pid} {row.name}"
                    self._timeline_metric %= len(TRACKED_METRICS)
                else:
                    self._append_event("Select a retained process with a known start time in view 2", "warn")
            self._timeline_pan = 0.0
            return True
        if key.lower() == "r":
            ranges = (None, *self.detector.window_seconds)
            self._timeline_range = ranges[(ranges.index(self._timeline_range) + 1) % len(ranges)]
            self._timeline_pan = 0.0
            return True
        if key in ("[", "]"):
            step = self._timeline_range or self.detector.window_seconds[0]
            self._timeline_pan = max(0.0, self._timeline_pan + (step if key == "[" else -step))
            return True
        if key.lower() == "f":
            self._toggle_freeze()
            return True
        steps = {UP: -1, DOWN: 1, PAGE_UP: -10, PAGE_DOWN: 10, HOME: -10**6, END: 10**6}
        if key in steps:
            self._timeline_offset = max(0, self._timeline_offset + steps[key])
            return True
        return False

    def _toggle_freeze(self) -> None:
        """Freeze or release the timeline view; monitoring is never affected."""
        if self._timeline_frozen is not None:
            self._timeline_frozen = None
            self._append_event("Timeline view released", "info")
            return
        snapshot = self.snapshot
        self._timeline_frozen = FrozenTimeline(
            frozen_at=time.time(),
            history=self._timeline_histories(),
            marks=self.timeline,
            alerts=snapshot.alerts if snapshot is not None and self._timeline_identity is None else (),
            sample_count=self._sample_count,
            scope=self._timeline_scope,
        )
        self._append_event("Timeline view frozen; sampling continues", "info")

    def _timeline_controls(self) -> list[tuple[str, str, str]]:
        palette, glyphs = self.palette, self.glyphs
        arrows = "<>" if glyphs.arrow == "->" else "←→"
        if self.console.size.width < 100:
            return [(arrows, "chart", palette.accent), ("F", "freeze", palette.warn),
                    ("P/R/[]", "scope/range", palette.accent)]
        return [
            (arrows, "chart", palette.accent),
            ("F", "live" if self.timeline_frozen else "freeze", palette.warn),
            ("N", "label", palette.teal),
            ("P/R/[]", "scope/range", palette.accent),
            (f"{glyphs.up}{glyphs.down}", "events", palette.muted),
        ]

    def _timeline_help_keys(self) -> list[tuple[str, str]]:
        return [
            ("Left/Right", "Chart private bytes, handles, threads, RSS, or CPU (Tab too)"),
            ("F", "Freeze or release this view only; sampling and the target continue"),
            ("M", "Add a numbered marker at the current time"),
            ("N", "Enter a marker label; Enter saves, Esc cancels (80 characters)"),
            ("P", "Tree/selected-process scope; choose the process in view 2 first"),
            ("R / [ / ]", "Cycle retained range / move one range earlier / later"),
            ("Up/Down", "Scroll the annotations listed under the chart"),
        ]

    def _render_timeline_view(self) -> Panel:
        palette, sep = self.palette, self.glyphs.sep
        frozen = self._timeline_frozen
        metric = self.timeline_metric
        title = Text(f" TIMELINE  {_TITLES[metric]} ", style=f"bold {palette.accent}")
        if self._timeline_identity is not None:
            title.append(self._timeline_scope + " ")
        if self._timeline_range is not None:
            title.append(f" {format_duration(self._timeline_range)} range ")
        if self._timeline_pan:
            title.append(f" {format_duration(self._timeline_pan)} before latest ")
        if frozen is not None:
            new = self._sample_count - frozen.sample_count
            title.append(
                f" FROZEN at {time.strftime('%H:%M:%S', time.localtime(frozen.frozen_at))} "
                f"{sep} sampling continues, {new:,} new sample{'s' if new != 1 else ''} ",
                style=f"bold {palette.ink} on {palette.warn}",
            )
        return Panel(
            Sized(self._timeline_body),
            title=title,
            title_align="left",
            subtitle=Text(" markers show timing, not cause ", style=palette.muted),
            subtitle_align="right",
            border_style=palette.warn if frozen is not None else palette.border,
            box=box.ROUNDED,
            padding=(0, 1),
        )

    def _timeline_data(self) -> tuple[tuple[tuple[float, float], ...], tuple[TimelineMark, ...],
                                      tuple[LeakAlert, ...]]:
        frozen = self._timeline_frozen
        metric = self.timeline_metric
        if frozen is not None:
            points, marks, alerts = frozen.history.get(metric, ()), frozen.marks, frozen.alerts
        else:
            snapshot = self.snapshot
            alerts = snapshot.alerts if snapshot is not None and self._timeline_identity is None else ()
            points, marks = self._timeline_histories().get(metric, ()), self.timeline
        if points and (self._timeline_range is not None or self._timeline_pan):
            span = self._timeline_range or max(0.0, points[-1][0] - points[0][0])
            end = max(points[0][0] + span, points[-1][0] - self._timeline_pan)
            end = min(points[-1][0], end)
            self._timeline_pan = max(0.0, points[-1][0] - end)
            points = tuple(point for point in points if end - span <= point[0] <= end)
        return points, marks, alerts

    def _timeline_histories(self) -> dict[str, tuple[tuple[float, float], ...]]:
        if self._timeline_identity is None:
            return {key: tuple(series) for key, series in self._history.items()}
        snapshot = self.snapshot
        points = self.detector.process_history.points(self._timeline_identity)
        until = snapshot.timestamp if snapshot is not None else float("inf")
        return {
            metric: tuple((stamp, values[metric]) for stamp, values in points
                          if stamp <= until and metric in values)[-self.HISTORY_LIMIT:]
            for metric in TRACKED_METRICS
        }

    def _timeline_body(self, width: int, height: int | None) -> RenderableType:
        palette, glyphs = self.palette, self.glyphs
        points, marks, alerts = self._timeline_data()
        metric = self.timeline_metric
        if len(points) < 2:
            if self._timeline_identity is None and self._timeline_range is None:
                return Text("Collecting samples for the timeline...", style=palette.muted)
            return Text("Unavailable or too little retained measured history in this scope/range. "
                        "Press P for tree scope or R to change range; sampling continues.",
                        style=palette.muted)
        height = height or 20
        start, end = points[0][0], points[-1][0]
        in_range = [mark for mark in marks if start <= mark.timestamp <= end]
        chart_width = max(8, width - Y_LABEL_WIDTH - 1)
        # Rows: chart, annotations, time axis, caption, legend; the rest lists events.
        chart_rows = max(3, min(12, (height - 4) // 2))
        interval = self.detector.sample_interval_ms / 1000.0
        columns, per_column = bucket_columns(
            points, start, end, chart_width, gap_seconds=GAP_INTERVALS * interval
        )
        alert = next((a for a in alerts if RESOURCE_METRICS[a.resource] == metric), None)
        window_start = end - alert.window_seconds if alert is not None else None
        lines: list[RenderableType] = self._chart_lines(columns, chart_rows, metric)
        lines.append(Text.assemble(
            (" " * (Y_LABEL_WIDTH + 1), ""),
            (mark_row(in_range, start, end, chart_width, window_start), f"bold {palette.warn}"),
        ))
        span = end - start
        axis = Text(no_wrap=True, overflow="crop")
        left = f"-{format_duration(span)}"
        right = "frozen" if self._timeline_frozen is not None else "latest"
        axis.append(" " * (Y_LABEL_WIDTH + 1))
        axis.append(left + " " * max(1, chart_width - len(left) - len(right)) + right,
                    style=palette.muted)
        lines.append(axis)
        lines.append(self._decimation_text(len(points), span, per_column))
        lines.append(self._legend_text(in_range, window_start, alert))
        remaining = height - len(lines)
        if remaining > 0:
            lines.append(self._mark_list(marks, remaining, width))
        return Group(*lines)

    def _chart_lines(self, columns: list[float | None], rows: int, metric: str) -> list[Text]:
        palette, glyphs = self.palette, self.glyphs
        present = [value for value in columns if value is not None]
        low, high = (min(present), max(present)) if present else (0.0, 1.0)
        if metric == "cpu_percent":
            low = min(low, 0.0)
        if high - low <= 0:
            high = low + (1.0 if metric != "private_bytes" else 1024.0 * 1024.0)
        ramp = glyphs.spark
        steps = len(ramp)
        levels = rows * steps
        scaled = [
            None if value is None else 1 + round((value - low) / (high - low) * (levels - 1))
            for value in columns
        ]
        lines = []
        for row in reversed(range(rows)):
            if row == rows - 1:
                label = self._axis_value(metric, high)
            elif row == 0:
                label = self._axis_value(metric, low)
            else:
                label = ""
            cells = []
            for level in scaled:
                if level is None:
                    cells.append(" ")
                    continue
                fill = level - row * steps
                cells.append(ramp[-1] if fill >= steps else (ramp[fill - 1] if fill > 0 else " "))
            lines.append(Text.assemble(
                (label.rjust(Y_LABEL_WIDTH)[:Y_LABEL_WIDTH] + " ", palette.muted),
                ("".join(cells), palette.accent),
                no_wrap=True,
            ))
        return lines

    @staticmethod
    def _axis_value(metric: str, value: float) -> str:
        if metric in ("private_bytes", "rss_bytes"):
            return format_bytes(value)
        if metric == "cpu_percent":
            return f"{value:.0f}%"
        return f"{value:,.0f}"

    def _decimation_text(self, samples: int, span: float, per_column: float) -> Text:
        sep = f" {self.glyphs.sep} "
        text = f"{samples:,} samples over {format_duration(span)}"
        if self._timeline_identity is not None or self._timeline_range is not None or self._timeline_pan:
            text += f"{sep}selected retained range; older/unreadable history may be unavailable"
            text += f"{sep}last {self.HISTORY_LIMIT} retained points at most; report charts may be thinned"
            if per_column > 1.05:
                text += f"{sep}~{per_column:.1f} samples per column, highest shown"
            return Text(text, style=self.palette.muted)
        if per_column > 1.05:
            text += f"{sep}~{per_column:.1f} samples per column, highest shown"
        else:
            text += f"{sep}one sample per column at most"
        frozen = self._timeline_frozen
        range_count = frozen.sample_count if frozen is not None else self._sample_count
        newer = self._sample_count - range_count
        if newer:
            text += f"{sep}{newer:,} newer samples collected while frozen"
        if range_count > samples:
            text += (
                f"{sep}earlier samples are not charted here (the dashboard keeps the last "
                f"{self.HISTORY_LIMIT}); report charts may be thinned; session facts remain exact"
            )
        return Text(text, style=self.palette.muted)

    def _legend_text(
        self, marks: Sequence[TimelineMark], window_start: float | None, alert: LeakAlert | None
    ) -> Text:
        palette = self.palette
        symbols = []
        for mark in marks:
            symbol = mark_symbol(mark)
            if symbol not in symbols:
                symbols.append(symbol)
        legend = Text(no_wrap=True, overflow="ellipsis")
        if window_start is not None and alert is not None:
            legend.append("[ ", style=f"bold {palette.warn}")
            legend.append(
                f"{format_duration(alert.window_seconds)} window start, baseline "
                f"{self._axis_value(alert.metric, alert.baseline_value)}  ",
                style=palette.muted,
            )
        for symbol in symbols:
            meaning = "marker" if symbol.isdigit() or symbol == "M" else _LEGEND.get(symbol, "")
            legend.append(f"{symbol} ", style=f"bold {palette.warn}")
            legend.append(f"{meaning}  ", style=palette.muted)
        if not len(legend):
            legend.append("no annotations in this range", style=palette.muted)
        return legend

    def _mark_list(self, marks: Sequence[TimelineMark], rows: int, width: int) -> Table:
        """Annotations newest first, scrolled with Up/Down."""
        palette = self.palette
        ordered = list(reversed(marks))
        self._timeline_offset = min(self._timeline_offset, max(0, len(ordered) - rows))
        shown = ordered[self._timeline_offset : self._timeline_offset + rows]
        table = Table.grid(padding=(0, 1), expand=True)
        table.add_column(width=8, no_wrap=True, style=palette.muted)
        table.add_column(width=1, no_wrap=True, style=f"bold {palette.warn}")
        table.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        if not shown:
            table.add_row("", "", Text("No annotations yet; press M to add a marker",
                                       style=palette.muted))
        for mark in shown:
            table.add_row(
                time.strftime("%H:%M:%S", time.localtime(mark.timestamp)),
                mark_symbol(mark),
                mark.label,
            )
        return table


__all__ = [
    "MARK_SYMBOLS",
    "TIMELINE_METRICS",
    "FrozenTimeline",
    "TimelineViewMixin",
    "bucket_columns",
    "mark_row",
    "mark_symbol",
]
