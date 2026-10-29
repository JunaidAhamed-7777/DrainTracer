"""View 2: the process inspector, or "which process deserves my attention?".

Lists every live process in the tree plus recently exited ones, ranks
them by growth over a chosen interval, and details the selected one. The
growth numbers come from the detector's per-process histories
(``ProcessHistory.growth``) and lifetime summaries; the view never
re-runs leak detection. Selection follows a process identity (PID plus
creation time) across refreshes, so a reused PID is never confused with
the process that was selected, and an exited selection stays readable.

Selecting a process changes nothing about the target controls: K still
ends the whole monitored tree, and the footer and help say so.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from rich import box
from rich.console import Group, RenderableType
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .events import (
    TRACKED_METRICS,
    LifecycleEvent,
    LifecycleKind,
    MetricsSnapshot,
    ProcessGrowth,
    ProcessMetrics,
    ProcessSummary,
)
from .keys import (
    BACKSPACE,
    BACKTAB,
    DOWN,
    END,
    ENTER,
    ESCAPE,
    HOME,
    LEFT,
    PAGE_DOWN,
    PAGE_UP,
    RIGHT,
    TAB,
    UP,
)
from .units import format_bytes, format_duration
from .widgets import Sized

Identity = tuple[int, "float | None"]

# Inspector orders cycled with S: the tree, or growth of one tracked metric.
INSPECTOR_ORDERS = ("tree", *TRACKED_METRICS)
_ORDER_LABELS = {
    "tree": "tree",
    "private_bytes": "private growth",
    "handles": "handle growth",
    "threads": "thread growth",
}
_SHORT_METRIC = {"private_bytes": "private", "handles": "handles", "threads": "threads"}
# Lifecycle events shown for the selected process.
DETAIL_EVENTS = 4
# Side-by-side list and detail from this width.
SPLIT_COLUMNS = 100


@dataclass(frozen=True)
class InspectorRow:
    """One line of the inspector list: a live process or an exited summary."""

    identity: Identity
    name: str
    connector: str = ""
    process: ProcessMetrics | None = None
    summary: ProcessSummary | None = None
    growth: dict[str, ProcessGrowth] = field(default_factory=dict)

    @property
    def pid(self) -> int:
        return self.identity[0]

    @property
    def exited(self) -> bool:
        return self.process is None


class ProcessesViewMixin:
    """Render and drive the process inspector for ``DashboardApp``."""

    def _init_processes_view(self) -> None:
        self._proc_selected: Identity | None = None
        self._proc_order = INSPECTOR_ORDERS[0]
        self._proc_interval = 0
        self._proc_focus = "list"
        self._proc_expanded = False
        self._proc_detail_offset = 0
        self._proc_scroll = 0
        self._proc_query = ""
        self._proc_searching = False
        self._proc_cache: tuple[tuple, dict[str, dict[Identity, ProcessGrowth]]] | None = None

    # ------------------------------------------------------------------
    # Keys
    # ------------------------------------------------------------------

    def _processes_key(self, key: str) -> bool:
        """Handle inspector keys; return whether the key was used here."""
        if self._proc_searching:
            return self._processes_search_key(key)
        if key == "/":
            self._proc_searching = True
            return True
        if key == ESCAPE and self._proc_query:
            self._proc_query = ""  # a second Esc returns to the overview
            return True
        if key.lower() == "s":
            position = INSPECTOR_ORDERS.index(self._proc_order)
            self._proc_order = INSPECTOR_ORDERS[(position + 1) % len(INSPECTOR_ORDERS)]
            return True
        if key in (LEFT, RIGHT):
            count = len(self._proc_intervals())
            self._proc_interval = (self._proc_interval + (1 if key == RIGHT else -1)) % count
            return True
        if key in (TAB, BACKTAB):
            self._proc_focus = "detail" if self._proc_focus == "list" else "list"
            return True
        if key == ENTER:
            self._proc_expanded = not self._proc_expanded
            self._proc_focus = "detail" if self._proc_expanded else "list"
            return True
        steps = {UP: -1, DOWN: 1, PAGE_UP: -10, PAGE_DOWN: 10, HOME: -10**6, END: 10**6}
        if key in steps:
            if self._proc_focus == "detail":
                self._proc_detail_offset = max(0, self._proc_detail_offset + steps[key])
            else:
                self._move_selection(steps[key])
            return True
        return False

    def _processes_search_key(self, key: str) -> bool:
        """While typing a filter every key edits it, so letters never run commands."""
        if key == ESCAPE:
            self._proc_query, self._proc_searching = "", False
        elif key == ENTER:
            self._proc_searching = False
        elif key == BACKSPACE:
            self._proc_query = self._proc_query[:-1]
        elif key in (UP, DOWN, PAGE_UP, PAGE_DOWN, HOME, END):
            self._proc_searching = False
            return self._processes_key(key)
        elif len(key) == 1 and key.isprintable() and len(self._proc_query) < 40:
            self._proc_query += key
        return True

    def _move_selection(self, step: int) -> None:
        rows = self._inspector_rows()
        if not rows:
            return
        index = self._selected_index(rows)
        if index is None and self._proc_selected is not None:
            index = 0  # the selection left every list: start again at the top
        else:
            # Without a selection yet, the first row is the implicit one.
            index = min(max(0, (index or 0) + step), len(rows) - 1)
        self._proc_selected = rows[index].identity
        self._proc_detail_offset = 0

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def _proc_intervals(self) -> tuple[float | None, ...]:
        """The detector's windows, then ``None`` for each process's lifetime."""
        return (*self.detector.window_seconds, None)

    def _proc_interval_label(self) -> str:
        interval = self._proc_intervals()[self._proc_interval]
        return "lifetime" if interval is None else format_duration(interval)

    def _growth_tables(self, snapshot: MetricsSnapshot) -> dict[str, dict[Identity, ProcessGrowth]]:
        """Per-metric growth by identity over the chosen interval, cached per sample."""
        interval = self._proc_intervals()[self._proc_interval]
        key = (snapshot.timestamp, interval, len(snapshot.exited_processes))
        if self._proc_cache is not None and self._proc_cache[0] == key:
            return self._proc_cache[1]
        tables: dict[str, dict[Identity, ProcessGrowth]] = {}
        for metric in TRACKED_METRICS:
            if interval is None:
                summaries = (*snapshot.process_summaries, *snapshot.exited_processes)
                rows = [growth for item in summaries if (growth := _lifetime(item, metric))]
            else:
                # End at the displayed sample: the detector thread may already
                # hold newer points than the snapshot the table shows.
                rows = list(
                    self.detector.process_history.growth(
                        metric, since=snapshot.timestamp - interval, until=snapshot.timestamp
                    )
                )
            tables[metric] = {row.identity: row for row in rows}
        self._proc_cache = (key, tables)
        return tables

    def _inspector_rows(self) -> list[InspectorRow]:
        snapshot = self.snapshot
        if snapshot is None:
            return []
        tables = self._growth_tables(snapshot)
        summaries = {item.identity: item for item in snapshot.process_summaries}

        def growth_of(identity: Identity) -> dict[str, ProcessGrowth]:
            return {
                metric: table[found]
                for metric, table in tables.items()
                if (found := _match(identity, table)) is not None
            }

        if self._proc_order == "tree":
            ordered = self._tree_order(snapshot)
        else:
            ordered = [(process, "") for process in snapshot.processes]
        live = [
            InspectorRow(
                identity=process.identity,
                name=process.name,
                connector=connector,
                process=process,
                summary=summaries.get(_match(process.identity, summaries) or process.identity),
                growth=growth_of(process.identity),
            )
            for process, connector in ordered
        ]
        live_ids = {row.identity for row in live}
        exited = [
            InspectorRow(
                identity=item.identity,
                name=item.name or "unknown",
                summary=item,
                growth=growth_of(item.identity),
            )
            for item in reversed(snapshot.exited_processes)
            if item.identity not in live_ids
        ]
        if self._proc_order != "tree":
            metric = self._proc_order

            def rank(row: InspectorRow) -> tuple:
                growth = row.growth.get(metric)
                return (growth is None, -(growth.delta if growth else 0.0), row.pid)

            live.sort(key=rank)
            exited.sort(key=rank)
        rows = live + exited
        query = self._proc_query.strip().lower()
        if query:
            rows = [
                row for row in rows if query in row.name.lower() or str(row.pid).startswith(query)
            ]
        return rows

    def _selected_index(self, rows: list[InspectorRow]) -> int | None:
        if self._proc_selected is None:
            return None
        for index, row in enumerate(rows):
            if _same(row.identity, self._proc_selected):
                return index
        return None

    def _selected_row(self, rows: list[InspectorRow]) -> InspectorRow | None:
        """The selected row; the first row when nothing (valid) was selected yet."""
        index = self._selected_index(rows)
        if index is not None:
            return rows[index]
        if self._proc_selected is None and rows:
            self._proc_selected = rows[0].identity
            return rows[0]
        return None

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _processes_controls(self) -> list[tuple[str, str, str]]:
        palette, glyphs = self.palette, self.glyphs
        controls = [
            (f"{glyphs.up}{glyphs.down}", "select", palette.accent),
            ("K", "kill whole tree", palette.danger),
            ("/", "search", palette.cyan),
            ("S", _ORDER_LABELS[self._proc_order], palette.cyan),
            ("<>" if glyphs.arrow == "->" else "←→", self._proc_interval_label(),
             palette.teal),
            ("TAB", "focus", palette.muted),
            ("ENTER", "detail", palette.muted),
        ]
        if self.manager.contains_monitor:
            controls.pop(1)
        return controls

    def _processes_help_keys(self) -> list[tuple[str, str]]:
        return [
            ("Up/Down", "Select a process (PgUp/PgDn/Home/End jump); with focus on the "
                        "detail, scroll it"),
            ("/", "Filter by name or PID; Enter keeps the filter, Esc clears it"),
            ("S", "Order: tree, then growth of private bytes, handles, or threads"),
            ("Left/Right", "Growth interval: the detection windows or each process's lifetime"),
            ("Tab", "Move focus between the list and the detail"),
            ("Enter", "Show the detail on its own"),
            ("K", "Still kills the whole monitored tree, never only the selected process"),
        ]

    def _render_processes_view(self) -> RenderableType:
        rows = self._inspector_rows()
        selected = self._selected_row(rows)
        width = self.console.size.width
        detail = self._render_process_detail(selected)
        if self._proc_expanded:
            return detail
        listing = self._render_process_list(rows, selected)
        layout = Layout(name="inspector")
        if width >= SPLIT_COLUMNS:
            layout.split_row(Layout(listing, ratio=3), Layout(detail, ratio=2))
        else:
            layout.split_column(Layout(listing, ratio=1), Layout(detail, ratio=1))
        return layout

    def _render_process_list(
        self, rows: list[InspectorRow], selected: InspectorRow | None
    ) -> Panel:
        palette, sep = self.palette, self.glyphs.sep
        live = sum(1 for row in rows if not row.exited)
        title = (
            f" PROCESSES {live} live {sep} {len(rows) - live} exited {sep} "
            f"{_ORDER_LABELS[self._proc_order]} {sep} {self._proc_interval_label()} "
        )
        focused = self._proc_focus == "list"
        return Panel(
            Sized(lambda width, height: self._process_list_table(rows, selected, width, height)),
            title=Text(title, style=f"bold {palette.accent_alt}"),
            title_align="left",
            subtitle=self._search_subtitle(),
            subtitle_align="right",
            border_style=palette.accent if focused else palette.border,
            box=box.ROUNDED,
            padding=(0, 1),
        )

    def _search_subtitle(self) -> Text | None:
        if not (self._proc_searching or self._proc_query):
            return None
        cursor = "_" if self._proc_searching else ""
        return Text(f" / {self._proc_query}{cursor} ", style=f"bold {self.palette.cyan}")

    def _process_list_table(
        self,
        rows: list[InspectorRow],
        selected: InspectorRow | None,
        width: int,
        height: int | None,
    ) -> RenderableType:
        palette, glyphs = self.palette, self.glyphs
        if not rows:
            message = (
                f"No process matches '{self._proc_query}'"
                if self._proc_query
                else "No processes sampled yet"
            )
            return Text(message, style=palette.muted)
        metric = self._proc_order if self._proc_order != "tree" else "private_bytes"
        # Optional columns give way before the process name does.
        rate = width >= 66
        handles_column = width >= 84
        table = Table(
            box=box.SIMPLE_HEAD,
            expand=True,
            show_edge=False,
            pad_edge=False,
            padding=(0, 1),
            header_style=f"bold {palette.muted}",
            border_style=palette.border,
        )
        table.add_column("", width=1, no_wrap=True)
        table.add_column("PID", justify="right", no_wrap=True)
        table.add_column("PROCESS", ratio=1, min_width=8, no_wrap=True, overflow="ellipsis")
        table.add_column("PRIVATE", justify="right", no_wrap=True)
        table.add_column(f"{glyphs.up}{_SHORT_METRIC[metric].upper()[:4]}", justify="right",
                         no_wrap=True)
        if rate:
            table.add_column("RATE", justify="right", no_wrap=True)
        if handles_column:
            table.add_column("HNDL", justify="right", no_wrap=True)
        if rate:
            table.add_column("STATUS", no_wrap=True)

        # Rows below the header; one more line goes to the position caption.
        capacity = max(1, (height or len(rows) + 2) - 2)
        if len(rows) > capacity:
            capacity = max(1, capacity - 1)
        index = next((i for i, row in enumerate(rows) if row is selected), 0)
        # Keep the selection visible: scroll only when it leaves the window.
        if index < self._proc_scroll:
            self._proc_scroll = index
        elif index >= self._proc_scroll + capacity:
            self._proc_scroll = index - capacity + 1
        self._proc_scroll = min(self._proc_scroll, max(0, len(rows) - capacity))
        shown = rows[self._proc_scroll : self._proc_scroll + capacity]
        for row in shown:
            is_selected = row is selected
            growth = row.growth.get(metric)
            name = Text.assemble(
                (row.connector, palette.border), (row.name, "bold" if is_selected else "")
            )
            process = row.process
            if process is None:
                private = self._format_last(row.summary, "private_bytes")
                handles = self._format_last(row.summary, "handles", count=True)
                status = Text("exited", style=palette.muted)
            else:
                private = (
                    format_bytes(process.private_bytes)
                    if process.measured("private_bytes") else "n/a"
                )
                handles = f"{process.handles:,}" if process.measured("handles") else "n/a"
                if process.access_denied:
                    status = Text("denied", style=palette.danger)
                else:
                    status = Text(process.status, style=self._process_status_style(process.status))
            cells: list[RenderableType] = [
                Text(">" if is_selected else " ", style=f"bold {palette.accent}"),
                str(row.pid),
                name,
                private,
                _signed(metric, growth.delta) if growth else "-",
            ]
            if rate:
                cells.append(_signed_rate(metric, growth.slope_per_second) if growth else "-")
            if handles_column:
                cells.append(handles)
            if rate:
                cells.append(status)
            style = f"on {palette.surface}" if is_selected else (palette.muted if row.exited else "")
            table.add_row(*cells, style=style)
        hidden = len(rows) - len(shown)
        if hidden > 0:
            first, last = self._proc_scroll + 1, self._proc_scroll + len(shown)
            table.caption = f"{first}-{last} of {len(rows)}"
            table.caption_style = palette.muted
        return table

    def _format_last(self, summary: ProcessSummary | None, metric: str, count: bool = False) -> str:
        if summary is None or metric not in summary.last:
            return "n/a"
        value = summary.last[metric]
        return f"{value:,.0f}" if count else format_bytes(value)

    def _render_process_detail(self, row: InspectorRow | None) -> Panel:
        palette = self.palette
        focused = self._proc_focus == "detail"
        content = self._process_detail_content(row)
        return Panel(
            Sized(lambda width, height: self._scrolled(
                content, width, height, "_proc_detail_offset", "end of detail"
            )),
            title=Text(" SELECTED PROCESS ", style=f"bold {palette.accent}"),
            title_align="left",
            subtitle=Text(" K kills the whole tree, not this process ", style=palette.muted),
            subtitle_align="right",
            border_style=palette.accent if focused else palette.border,
            box=box.ROUNDED,
            padding=(0, 1),
        )

    def _process_detail_content(self, row: InspectorRow | None) -> RenderableType:
        palette, sep = self.palette, f" {self.glyphs.sep} "
        if row is None:
            if self._proc_selected is not None:
                pid = self._proc_selected[0]
                return Text(
                    f"PID {pid} is no longer tracked: it exited and its summary was dropped "
                    "(the inspector keeps the most recent exited processes). Press Up or "
                    "Down to select another.",
                    style=palette.muted,
                )
            return Text("No process selected", style=palette.muted)

        grid = Table.grid(padding=(0, 2), expand=True)
        grid.add_column(style=f"bold {palette.muted}", no_wrap=True, min_width=9)
        grid.add_column(ratio=1, overflow="fold")
        process, summary = row.process, row.summary
        status = "exited" if process is None else ("access denied" if process.access_denied
                                                   else process.status)
        title = Text.assemble(
            (row.name, "bold"),
            (f"  PID {row.pid}  ", palette.muted),
            (status.upper(), f"bold {palette.muted if process is None else palette.ok}"),
        )
        grid.add_row("Identity", self._identity_text(row))
        grid.add_row("Parent", self._parent_text(process.ppid if process else
                                                 (summary.ppid if summary else None)))
        if process is not None:
            self._live_detail_rows(grid, row, process, sep)
        elif summary is not None:
            self._exited_detail_rows(grid, summary, sep)
        grid.add_row(f"Growth {self._proc_interval_label()}", self._growth_text(row, sep))
        grid.add_row("Events", self._events_text(row))
        tree = self._tree_context(row, sep)
        if tree is not None:
            grid.add_row("Tree", tree)
        return Group(title, grid)

    def _identity_text(self, row: InspectorRow) -> str:
        sep = f" {self.glyphs.sep} "
        created = row.identity[1]
        summary = row.summary
        parts = []
        if created is not None:
            parts.append(f"started {time.strftime('%H:%M:%S', time.localtime(created))}")
            end = summary.exited_at if summary and summary.exited_at else time.time()
            parts.append(f"{'lived' if row.exited else 'alive'} {format_duration(end - created)}")
        else:
            parts.append("start time unknown")
        if summary is not None:
            parts.append(f"seen {summary.samples:,} samples")
        return sep.join(parts)

    def _parent_text(self, ppid: int | None) -> Text:
        if ppid is None:
            return Text("none recorded")
        snapshot = self.snapshot
        parent = next((p for p in snapshot.processes if p.pid == ppid), None) if snapshot else None
        if parent is not None:
            return Text(f"PID {ppid} {parent.name}")
        return Text(f"PID {ppid} (outside the monitored tree or exited)")

    def _live_detail_rows(self, grid: Table, row: InspectorRow, process: ProcessMetrics,
                          sep: str) -> None:
        if process.access_denied:
            grid.add_row("Counters", Text("access denied: every counter is unknown, not zero",
                                          style=f"bold {self.palette.danger}"))
            return

        def value(metric: str, text: str) -> str:
            return text if process.measured(metric) else "n/a"

        grid.add_row("Memory", sep.join((
            f"private {value('private_bytes', format_bytes(process.private_bytes))}",
            f"RSS {value('rss_bytes', format_bytes(process.rss_bytes))}",
            f"virtual {value('vms_bytes', format_bytes(process.vms_bytes))}",
        )))
        grid.add_row("Counts", sep.join((
            f"handles {value('handles', f'{process.handles:,}')}",
            f"threads {value('threads', f'{process.threads:,}')}",
        )))
        grid.add_row("CPU / I/O", sep.join((
            f"CPU {value('cpu_percent', f'{process.cpu_percent:.1f}%')}",
            f"read {value('io_read_bytes_per_sec', format_bytes(process.io_read_bytes_per_sec))}/s",
            f"write {value('io_write_bytes_per_sec', format_bytes(process.io_write_bytes_per_sec))}/s",
        )))
        if process.unmeasured:
            grid.add_row("Unreadable", Text(", ".join(process.unmeasured),
                                            style=f"bold {self.palette.warn}"))

    def _exited_detail_rows(self, grid: Table, summary: ProcessSummary, sep: str) -> None:
        stamp = lambda value: time.strftime("%H:%M:%S", time.localtime(value))  # noqa: E731
        lifetime = f"seen {stamp(summary.first_seen)} to {stamp(summary.last_seen)}"
        if summary.exited_at is not None:
            lifetime += f"{sep}gone by {stamp(summary.exited_at)}"
        grid.add_row("Lifetime", lifetime)
        for metric in TRACKED_METRICS:
            if metric not in summary.last:
                continue
            show = format_bytes if metric == "private_bytes" else (lambda v: f"{v:,.0f}")
            grid.add_row(
                _SHORT_METRIC[metric].capitalize(),
                f"first {show(summary.first[metric])}{sep}last {show(summary.last[metric])}"
                f"{sep}peak {show(summary.peak[metric])}",
            )

    def _growth_text(self, row: InspectorRow, sep: str) -> Text:
        parts = []
        for metric in TRACKED_METRICS:
            growth = row.growth.get(metric)
            if growth is None:
                continue
            parts.append(
                f"{_SHORT_METRIC[metric]} {_signed(metric, growth.delta)} "
                f"({_signed_rate(metric, growth.slope_per_second)})"
            )
        if not parts:
            return Text("no measured history in this interval", style=self.palette.muted)
        growth = next(iter(row.growth.values()))
        note = f"{sep}over {format_duration(growth.span_seconds)}, {growth.samples} samples"
        if growth.new_process and self._proc_intervals()[self._proc_interval] is not None:
            note += ", started inside the interval"
        return Text(sep.join(parts) + note)

    def _events_text(self, row: InspectorRow) -> Text:
        events = self.lifecycle_for(row.pid, row.identity[1])[-DETAIL_EVENTS:]
        if not events:
            return Text("no starts, exits or restarts seen", style=self.palette.muted)
        return Text("; ".join(_event_text(event, row.pid) for event in events))

    def _tree_context(self, row: InspectorRow, sep: str) -> str | None:
        """Tree totals as context, with this process's share of private bytes."""
        snapshot = self.snapshot
        if snapshot is None:
            return None
        totals = snapshot.totals
        private = totals.get("private_bytes", 0.0)
        text = (
            f"{len(snapshot.processes)} processes{sep}private {format_bytes(private)}{sep}"
            f"handles {totals.get('handles', 0.0):,.0f}{sep}threads {totals.get('threads', 0.0):,.0f}"
        )
        process = row.process
        if process is not None and private > 0 and process.measured("private_bytes"):
            text += f"{sep}this process {process.private_bytes / private:.0%} of private"
        return text


def _same(left: Identity, right: Identity) -> bool:
    """Identities match on PID and, when both are known, creation time."""
    if left[0] != right[0]:
        return False
    if left[1] is None or right[1] is None:
        return True
    return abs(left[1] - right[1]) <= 1e-3


def _match(identity: Identity, table: dict) -> Identity | None:
    if identity in table:
        return identity
    return next((key for key in table if _same(key, identity)), None)


def _lifetime(summary: ProcessSummary, metric: str) -> ProcessGrowth | None:
    """Whole-lifetime growth from a process summary (first to last measured value)."""
    delta = summary.growth(metric)
    if delta is None:
        return None
    span = max(0.0, summary.last_seen - summary.first_seen)
    return ProcessGrowth(
        pid=summary.pid,
        create_time=summary.create_time,
        name=summary.name,
        ppid=summary.ppid,
        metric=metric,
        first_value=summary.first[metric],
        last_value=summary.last[metric],
        delta=delta,
        slope_per_second=delta / span if span > 0 else 0.0,
        samples=summary.samples,
        span_seconds=span,
        exited=summary.exited,
    )


def _signed(metric: str, value: float) -> str:
    if metric == "private_bytes":
        return f"{'-' if value < 0 else '+'}{format_bytes(abs(value))}"
    return f"{value:+,.0f}"


def _signed_rate(metric: str, value: float) -> str:
    if metric == "private_bytes":
        return f"{'-' if value < 0 else '+'}{format_bytes(abs(value))}/s"
    return f"{value:+,.1f}/s"


def _event_text(event: LifecycleEvent, pid: int) -> str:
    clock = time.strftime("%H:%M:%S", time.localtime(event.timestamp))
    if event.kind == LifecycleKind.RESTARTED and event.pid != pid:
        return f"{clock} replaced by PID {event.pid}"
    if event.kind == LifecycleKind.RESTARTED:
        return f"{clock} restarted (replaces PID {event.replaces_pid})"
    return f"{clock} {event.kind.value}"


__all__ = ["INSPECTOR_ORDERS", "InspectorRow", "ProcessesViewMixin"]
