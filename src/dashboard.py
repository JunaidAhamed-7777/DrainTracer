"""Rich terminal dashboard for DrainTracer."""

from __future__ import annotations

import logging
import math
import os
import time
import webbrowser
from pathlib import Path

import psutil
from rich import box
from rich.console import Console, Group, RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .events import (
    EvidenceState,
    LeakAlert,
    LeakResource,
    LeakSeverity,
    MetricsSnapshot,
    ProcessMetrics,
    ResourceEvidence,
    severity_rank,
    unavailable_reason,
)
from .config import MonitorSettings
from .handle_types import describe_type_growth
from .keys import (
    DOWN,
    END,
    ESCAPE,
    HOME,
    PAGE_DOWN,
    PAGE_UP,
    UP,
    KeyReader,
    normalize_key,
)
from .report import DEFAULT_REPORT_DIR
from .session import MonitorSession
from .theme import PALETTE
from .units import format_duration
from .view_evidence import EvidenceViewMixin
from .view_monitor import MonitorViewMixin
from .view_processes import ProcessesViewMixin
from .view_timeline import TimelineViewMixin
from .widgets import LegacySafe, Lines, Sized, Sparkline


logger = logging.getLogger(__name__)

# Process table orders cycled with S.
SORT_MODES = ("tree", "memory", "handles", "cpu")

# Numbered views: the key selects the view, the label names its tab.
VIEWS = {
    "1": ("overview", "OVERVIEW", "OVR"),
    "2": ("processes", "PROCESSES", "PROC"),
    "3": ("evidence", "EVIDENCE", "EVID"),
    "4": ("timeline", "TIMELINE", "TIME"),
    "5": ("monitor", "MONITOR", "MON"),
}
VIEW_NAMES = tuple(name for name, _label, _short in VIEWS.values())
# Lines the help panel scrolls per key; Home jumps back to the top.
HELP_SCROLL = {UP: -1, DOWN: 1, PAGE_UP: -10, PAGE_DOWN: 10, HOME: -10**6, END: 10**6}


class DashboardApp(
    ProcessesViewMixin, EvidenceViewMixin, TimelineViewMixin, MonitorViewMixin, MonitorSession
):
    """Render a monitor session live in the terminal and handle its controls.

    The overview shows everything at a glance; numbered views focus on one
    question each. Views only render what the session and detector already
    produced, and switching views never changes monitoring.
    """

    VERSION = "v5.2.1"
    DELTA_WINDOW_SECONDS = 10.0
    FRAME_SECONDS = 0.1
    # Redraw at least this often so the clock and countdowns keep moving.
    CLOCK_SECONDS = 1.0

    KILL_CONFIRM_SECONDS = 3.0
    EVENT_LINES = 2

    def __init__(
        self,
        target_path: str | None,
        args: list[str] | None = None,
        console: Console | None = None,
        report_dir: str | Path = DEFAULT_REPORT_DIR,
        attach_pid: int | None = None,
        timeout: float | None = None,
        settings: MonitorSettings | None = None,
    ) -> None:
        super().__init__(
            target_path,
            args=args,
            console=console,
            report_dir=report_dir,
            attach_pid=attach_pid,
            timeout=timeout,
            settings=settings,
        )
        self.palette = PALETTE
        self._spark_rows = 2
        self._health_spacious = True
        self._quit_requested = False
        self._loop_errors: set[str] = set()
        self._show_help = False
        self._help_offset = 0
        self._sort_mode = SORT_MODES[0]
        self._kill_armed_until = 0.0
        self._cpu_cores = psutil.cpu_count(logical=True) or 1
        self._keys = KeyReader()
        self._view = "overview"
        self._init_processes_view()
        self._init_evidence_view()
        self._init_timeline_view()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> int:
        """Start monitoring and run the interactive dashboard until quit."""
        palette = self.palette
        try:
            with self.console.status(
                f"[bold {palette.accent}]Attaching monitor[/] "
                f"[{palette.muted}]initializing process telemetry...[/]",
                spinner="dots",
                spinner_style=palette.accent,
            ):
                self.start()
        except Exception as exc:
            message = Text.assemble(
                ("Unable to start monitor\n\n", f"bold {palette.danger}"), (str(exc), "")
            )
            hint = self.startup_hint(exc)
            if hint:
                message.append(f"\n\n{hint}", style=palette.muted)
            self.console.print(
                Panel(
                    message,
                    title=Text(" STARTUP ERROR ", style=f"bold {palette.danger}"),
                    title_align="left",
                    border_style=palette.danger,
                    box=box.ROUNDED,
                    padding=(1, 2),
                )
            )
            return 1

        try:
            with Live(
                self._frame(),
                console=self.console,
                auto_refresh=False,
                screen=self.console.is_terminal,
                transient=False,
            ) as live:
                # Keys are read every frame, but the layout is only rebuilt
                # when something changed: a snapshot, a key, a resize, or the
                # once-a-second clock. Rendering every frame cost far more
                # CPU than sampling.
                last_render = time.monotonic()
                last_size = self.console.size
                while not self._quit_requested:
                    changed = self._guarded("Processing samples", self.poll) or False
                    key = self._read_key()
                    if key:
                        self._guarded(f"Key {key!r}", self._handle_key, key)
                        changed = True
                    if self.timed_out and not self._quit_requested:
                        self._quit_requested = True
                        self._append_event(
                            f"Time limit of {format_duration(self.timeout or 0.0)} reached",
                            "info",
                        )
                    now = time.monotonic()
                    size = self.console.size
                    if changed or size != last_size or now - last_render >= self.CLOCK_SECONDS:
                        self._guarded("Drawing the dashboard", self._refresh, live)
                        last_render, last_size = now, size
                    time.sleep(self.FRAME_SECONDS)
        except KeyboardInterrupt:
            self._append_event("Dashboard interrupted by user", "warn")
        finally:
            self.shutdown()

        try:
            self.console.print(self.render_summary())
        except Exception as exc:
            # The session itself succeeded; still say where the report went.
            logger.exception("Session summary failed")
            self.console.print(Text(f"Session summary unavailable: {exc}"))
            if self._last_report is not None:
                self.console.print(Text(f"Report: {self._last_report}"))
        return 0

    def _refresh(self, live: Live) -> None:
        live.update(self._frame(), refresh=True)

    def _frame(self) -> RenderableType:
        """The dashboard, or a plain notice while it cannot be drawn.

        A stale frame would suggest the target went quiet; the notice says
        monitoring continues and which keys still work.
        """
        try:
            return self._render()
        except Exception as exc:
            self._note_loop_error("Drawing the dashboard", exc)
            return Text.assemble(
                (f"{self.TITLE} could not draw its dashboard: ", f"bold {self.palette.danger}"),
                str(exc),
                ("\nMonitoring continues. Press E to export a report or Q to quit.",
                 self.palette.muted),
            )

    def _guarded(self, action: str, function, *args):
        """Run one step of the live loop; log a failure instead of ending the session.

        A display or key bug must not stop a long session and take the
        target down with it. Each distinct failure is logged once; Ctrl+C
        still propagates.
        """
        try:
            return function(*args)
        except Exception as exc:
            self._note_loop_error(action, exc)
            return None

    def _note_loop_error(self, action: str, exc: Exception) -> None:
        message = f"{action} failed: {exc}"
        if message not in self._loop_errors:
            self._loop_errors.add(message)
            logger.exception("%s failed", action)
            self._append_event(message, "error")

    def _handle_key(self, key: str | None) -> None:
        if not key:
            return
        key = normalize_key(key)
        normalized = key.lower()

        if self._show_help and key == ESCAPE:
            self._show_help = False
            return
        if self._show_help and key in HELP_SCROLL:
            self._help_offset = max(0, self._help_offset + HELP_SCROLL[key])
            return
        if not self._show_help:
            # The active view sees keys first: navigation, and text while searching.
            handler = getattr(self, f"_{self._view}_key", None)
            if handler is not None and handler(key):
                return
        if key == ESCAPE:
            self._set_view("overview")
        elif key == "?" or normalized == "h":
            self._show_help = not self._show_help
            self._help_offset = 0
        elif key in VIEWS:
            self._show_help = False
            self._set_view(VIEWS[key][0])
        elif normalized == "s":
            position = SORT_MODES.index(self._sort_mode)
            self._sort_mode = SORT_MODES[(position + 1) % len(SORT_MODES)]
        elif key == " ":
            if self.detector.is_paused:
                if self.manager.resume():
                    self.detector.resume()
                    self._append_event("Monitoring resumed", "ok")
                else:
                    self._append_event(
                        f"Resume failed: {self.manager.last_error or 'unknown error'}",
                        "error",
                    )
            elif self.manager.contains_monitor:
                self._append_event(
                    "Pause is unavailable: DrainTracer runs inside this process tree", "warn"
                )
            else:
                if self.manager.pause():
                    self.detector.pause()
                    self._append_event("Monitoring paused", "warn")
                else:
                    self._append_event(
                        f"Pause failed: {self.manager.last_error or 'unknown error'}",
                        "error",
                    )
        elif normalized == "k":
            if self.manager.contains_monitor:
                self._append_event(
                    "Kill is unavailable: DrainTracer runs inside this process tree", "warn"
                )
                return
            if not (self.manager.is_running or self.manager.has_tracked_processes):
                self._append_event("Nothing to kill: the target tree has already exited", "info")
                return
            # Attached processes were not started by DrainTracer (a service, a
            # server); require a second press before ending them.
            now = time.monotonic()
            if self.attached and now > self._kill_armed_until:
                self._kill_armed_until = now + self.KILL_CONFIRM_SECONDS
                self._append_event(
                    f"Press K again within {self.KILL_CONFIRM_SECONDS:.0f}s to kill "
                    f"attached PID {self.manager.root_pid} and its children",
                    "warn",
                )
                return
            self._kill_armed_until = 0.0
            self._mark_stopping()
            self.manager.kill()
            if self.manager.has_tracked_processes:
                self._append_event(
                    f"Target cleanup incomplete: {self.manager.last_error or 'unknown error'}",
                    "error",
                )
            else:
                self._append_event("Target process tree terminated", "warn")
        elif normalized == "e":
            try:
                path = self.export_log()
            except Exception as exc:  # a failed export must never end the session
                self._append_event(f"Export failed: {exc}", "error")
            else:
                formats = "JSON + HTML" if self._last_html_report else "JSON"
                self._append_event(
                    f"Report {self._last_report_id} saved to {path.parent} ({formats})"
                    + ("; press O to open it" if self._last_html_report else ""),
                    "ok",
                )
        elif normalized == "o":
            self._open_last_report()
        elif normalized == "m":
            # Works in every view; marks timing only, it does not touch the target.
            self.add_marker()
        elif normalized == "q":
            self._quit_requested = True
            self._append_event("Quit requested", "info")

    def _set_view(self, view: str) -> None:
        """Switch the focused view; monitoring and the target are unaffected."""
        if view in VIEW_NAMES:
            self._view = view
            opened = getattr(self, f"_show_{view}", None)
            if opened is not None:
                opened()

    @property
    def view(self) -> str:
        """The focused view's name, such as ``overview`` or ``monitor``."""
        return self._view

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    WIDE_LAYOUT_COLUMNS = 110
    COMPACT_LAYOUT_ROWS = 30
    SPLIT_BOTTOM_COLUMNS = 80
    # Focused views add the event log below them from this height.
    VIEW_EVENT_ROWS = 36
    VIEW_HELP = {
        "overview": "resource cards, process tree, leak health and events",
        "processes": "select, search and rank processes by growth; exited ones stay listed",
        "evidence": "why a signal was or was not raised: each window's checks and values",
        "timeline": "one chart at a time with process, pause, signal and marker annotations",
        "monitor": "sampling cadence and cost, coverage, freshness, monitor footprint",
    }

    def _render(self) -> RenderableType:
        """The current frame, made printable on legacy code pages."""
        layout = self._render_layout()
        encoding = self.console.encoding
        return LegacySafe(layout, encoding) if LegacySafe.needed(encoding) else layout

    def _render_layout(self) -> Layout:
        if self._show_help:
            layout = Layout(name="root")
            layout.split_column(
                Layout(self._render_header(), name="header", size=3),
                Layout(self._render_help(), name="body", ratio=1),
                Layout(self._render_footer(), name="footer", size=1),
            )
            return layout
        if self._view != "overview":
            return self._render_focused_view()
        width, height = self.console.size
        wide = width >= self.WIDE_LAYOUT_COLUMNS
        compact = height < self.COMPACT_LAYOUT_ROWS
        process_rows = len(self.snapshot.processes) if self.snapshot else 1

        # Spare rows go to taller charts; short terminals get a metric strip.
        self._health_spacious = wide and not compact
        health_height = 9 if self._health_spacious else 7
        if compact:
            metrics = Layout(self._render_metrics_strip(), name="metrics", size=6)
        else:
            if wide:
                card_height = (height - 4 - (process_rows + 5)) // 2
            else:
                card_height = (height - 4 - health_height - 8) // 2
            card_height = max(6, min(card_height, 10))
            self._spark_rows = card_height - 4
            metrics = Layout(name="metrics", size=card_height * 2)
            metrics.split_column(Layout(name="cards_top"), Layout(name="cards_bottom"))
            metrics["cards_top"].split_row(
                Layout(self._render_cpu_card()),
                Layout(self._render_memory_card()),
            )
            metrics["cards_bottom"].split_row(
                Layout(self._render_handles_card()),
                Layout(self._render_threads_card()),
            )

        layout = Layout(name="root")
        layout.split_column(
            Layout(self._render_header(), name="header", size=3),
            Layout(name="body", ratio=1),
            Layout(self._render_footer(), name="footer", size=1),
        )
        health = Layout(self._render_health_panel(), name="health", size=health_height)
        tree = Layout(self._render_process_panel(), name="tree", ratio=3)
        events = Layout(self._render_event_panel(), name="events", ratio=2)

        if wide:
            layout["body"].split_row(Layout(name="main", ratio=3), Layout(name="side", ratio=2))
            layout["main"].split_column(metrics, tree)
            layout["side"].split_column(health, events)
            return layout

        bottom = Layout(name="bottom", ratio=1)
        if width >= self.SPLIT_BOTTOM_COLUMNS:
            bottom.split_row(tree, events)
        elif not compact:
            bottom.split_column(tree, events)
        else:
            # Too small for both; the header still shows the process count.
            bottom.update(self._render_event_panel())
        layout["body"].split_column(metrics, health, bottom)
        return layout

    def _render_focused_view(self) -> Layout:
        """One view with a compact status line, plus context on larger terminals.

        At 80x24 every row but the status line and the footer goes to the
        view; tall terminals get the full header, the view tabs, and the
        event log as supporting context.
        """
        width, height = self.console.size
        roomy = height >= self.COMPACT_LAYOUT_ROWS
        body = getattr(self, f"_render_{self._view}_view")()
        parts = [Layout(self._render_header(), name="header", size=3)] if roomy else []
        parts.append(Layout(self._render_status_line(), name="status", size=1))
        parts.append(Layout(body, name="body", ratio=1))
        if roomy and height >= self.VIEW_EVENT_ROWS:
            parts.append(Layout(self._render_event_panel(), name="events", size=7))
        parts.append(Layout(self._render_footer(), name="footer", size=1))
        layout = Layout(name="root")
        layout.split_column(*parts)
        return layout

    def _render_status_line(self) -> Table:
        """View tabs on the left; run state, signal, and sample age on the right."""
        palette, glyphs = self.palette, self.glyphs
        width = self.console.size.width
        tabs = Text(no_wrap=True, overflow="ellipsis")
        for key, (name, label, short) in VIEWS.items():
            if name == self._view:
                tabs.append(f" {key} {label} ", style=f"bold {palette.ink} on {palette.accent}")
            else:
                tabs.append(f" {key} {short if width < 110 else label.title()} ",
                            style=palette.muted)
        status, status_color = self._status(self.snapshot)
        right = Text(no_wrap=True)
        if self.timeline_frozen:
            # The view is frozen, not monitoring: say so wherever the user is.
            right.append(" TIMELINE FROZEN ", style=f"bold {palette.ink} on {palette.warn}")
            right.append(" ")
        right.append(f" {glyphs.dot} {status} ", style=f"bold {palette.ink} on {status_color}")
        snapshot = self.snapshot
        if snapshot is not None and snapshot.severity != LeakSeverity.NONE:
            right.append(
                f" {glyphs.warn} {snapshot.severity.value.upper()}",
                style=f"bold {self._severity_color(snapshot.severity)}",
            )
        if snapshot is not None:
            age = max(0.0, time.time() - snapshot.timestamp)
            right.append(f" {format_duration(age) if age >= 60 else f'{age:.1f}s'} ",
                         style=palette.muted)
        bar = Table.grid(expand=True)
        bar.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        bar.add_column(justify="right", no_wrap=True)
        bar.add_row(tabs, right)
        return bar

    def _render_help(self) -> Panel:
        """Keys, how to read a signal, and the settings this session runs with."""
        palette, glyphs = self.palette, self.glyphs

        def section(title: str) -> Text:
            return Text(title, style=f"bold {palette.muted}")

        def grid() -> Table:
            table = Table.grid(padding=(0, 2))
            table.add_column(style=f"bold {palette.accent}", no_wrap=True, min_width=10)
            table.add_column(overflow="fold")
            return table

        keys = grid()
        kill = "Kill the monitored process tree"
        if self.attached:
            kill += " (press twice: DrainTracer did not start it)"
        quit_text = (
            "Detach and quit; the process keeps running"
            if self.attached
            else "Quit and stop the target"
        )
        for key, meaning in (
            ("Space", "Pause or resume the target and sampling"),
            ("K", kill),
            ("E", "Export the JSON and HTML report"),
            ("O", "Open the latest HTML report in the browser"),
            (
                "S",
                "Sort processes by tree, memory (private bytes), handles, or CPU "
                f"(now: {self._sort_label()})",
            ),
            ("M", "Add a numbered marker to the event log, timeline and report "
                  "(marks timing, not cause)"),
            ("? / H", "Show or hide this help; Esc closes it"),
            ("Q", quit_text),
        ):
            keys.add_row(key, meaning)

        views = grid()
        for key, (name, label, _short) in VIEWS.items():
            views.add_row(key, f"{label.title()}: {self.VIEW_HELP[name]}")
        views.add_row(
            "Esc",
            "Back to the overview (closes this help first). Views change only what is "
            "shown; sampling and the target are unaffected",
        )
        view_keys = getattr(self, f"_{self._view}_help_keys", lambda: [])()
        in_view = grid()
        for key, meaning in view_keys:
            in_view.add_row(key, meaning)

        signals = Text(
            "A leak signal means private memory, handles, or threads kept growing "
            "across a window: an upward trend, at least 65% of steps not falling, "
            "and at most 15% of samples below the running peak. Severity rises with "
            "the amount and rate of growth relative to the thresholds below. Signals "
            "are heuristics that point at growth worth investigating, not proof of a "
            "leak. Without a signal, COLLECT means the shortest window is still filling "
            "and STEADY that a window was judged with no sustained growth. PARTIAL means "
            "some processes could not be read: totals leave them out, and a change in "
            "which ones restarts the windows.",
        )
        counters = Text(
            "PRIVATE is private committed memory: bytes a process alone has "
            "reserved backing for, in RAM or the page file. The memory signal "
            "judges this counter. RSS (working set) is the memory resident in RAM "
            "right now, shared pages included; Windows trims and refills it, so it "
            "is shown for context only. Growth in either is not proof of an "
            "application allocation leak: caches, pools, and fragmentation grow too.",
        )

        session = grid()
        detector = self.detector
        windows = " / ".join(format_duration(window) for window in detector.window_seconds)
        session.add_row("Windows", windows)
        session.add_row("Sampling", f"every {detector.sample_interval_ms} ms")
        freshness = self.freshness_text()
        if freshness:
            # The same line as under LEAK HEALTH: turns amber when samples go stale.
            session.add_row("Monitor", freshness)
        session.add_row(
            "Warm-up",
            format_duration(detector.warmup_seconds) if detector.warmup_seconds else "none",
        )
        limits = detector.thresholds
        memory = limits[LeakResource.MEMORY]
        handles = limits[LeakResource.HANDLES]
        threads = limits[LeakResource.THREADS]
        session.add_row(
            "Thresholds",
            f"private {self._format_bytes(memory.critical_delta)}, "
            f"{self._format_bytes(memory.critical_slope)}/s {glyphs.sep} "
            f"handles {handles.critical_delta:,.0f}, {handles.critical_slope:g}/s {glyphs.sep} "
            f"threads {threads.critical_delta:,.0f}, {threads.critical_slope:g}/s",
        )
        # Paths and names are user data: Text keeps Rich from reading them as markup.
        session.add_row("Settings", Text(self.settings.describe()))
        session.add_row("Reports", Text(os.path.abspath(self.report_dir)))

        parts: list[RenderableType] = [section("KEYS"), keys, Text("")]
        if view_keys:
            label = next(label for name, label, _s in VIEWS.values() if name == self._view)
            parts += [section(f"IN {label}"), in_view, Text("")]
        parts += [
            section("VIEWS"), views, Text(""),
            section("THIS SESSION"), session, Text(""),
            section("READING A SIGNAL"), signals, Text(""),
            section("MEMORY COUNTERS"), counters,
        ]
        content = Group(*parts)
        return Panel(
            Sized(lambda width, height: self._scrolled(content, width, height)),
            title=Text(" HELP ", style=f"bold {palette.accent}"),
            title_align="left",
            subtitle=Text(f" {glyphs.up}{glyphs.down} scroll {glyphs.sep} ? or Esc to close ",
                          style=palette.muted),
            subtitle_align="right",
            border_style=palette.border,
            box=box.ROUNDED,
            padding=(0, 1),
        )

    def _scrolled(
        self,
        content: RenderableType,
        width: int,
        height: int | None,
        offset_attr: str = "_help_offset",
        end_note: str = "end of help",
    ) -> RenderableType:
        """``content`` from the offset in ``offset_attr``, clamped, noting what follows."""
        options = self.console.options.update(width=width, height=None)
        lines = self.console.render_lines(content, options, pad=False)
        if height is None or len(lines) <= height:
            setattr(self, offset_attr, 0)
            return Lines(lines)
        offset = min(getattr(self, offset_attr), len(lines) - height + 1)
        setattr(self, offset_attr, offset)
        shown = lines[offset : offset + height - 1]
        below = len(lines) - offset - len(shown)
        note = Text(
            f"{self.glyphs.down} {below} more line{'s' if below != 1 else ''}"
            if below > 0 else end_note,
            style=self.palette.muted,
        )
        return Group(Lines(shown), note)

    def _render_header(self) -> RenderableType:
        palette, glyphs = self.palette, self.glyphs
        status, status_color = self._status(self.snapshot)
        roomy = self.console.size.width >= 90

        brand = Text.assemble(
            (f" {glyphs.brand} {self.TITLE} ", f"bold {palette.ink} on {palette.accent}"),
            (f" {self.VERSION} ", f"bold {palette.accent} on {palette.surface}"),
            ("  resource leak monitor" if roomy else "", palette.muted),
        )
        right = Text.assemble(
            (" TIMELINE FROZEN " if self.timeline_frozen else "",
             f"bold {palette.ink} on {palette.warn}"),
            (" " if self.timeline_frozen else "", ""),
            (f" {glyphs.dot} {status} ", f"bold {palette.ink} on {status_color}"),
            (f"  {time.strftime('%H:%M:%S')} ", palette.muted),
        )
        bar = Table.grid(expand=True)
        bar.add_column(ratio=1, no_wrap=True)
        bar.add_column(justify="right", no_wrap=True)
        bar.add_row(brand, right)

        snapshot = self.snapshot
        pid = snapshot.root_pid if snapshot and snapshot.root_pid else "-"
        process_count = len(snapshot.processes) if snapshot else 0
        processes = str(process_count)
        counts = snapshot.process_counts if snapshot else None
        if counts is not None and (counts.started or counts.exited):
            # Churn next to the count: a steady count can hide restarts.
            churn = f"+{counts.started}/-{counts.exited}"
            processes += f" ({churn} started/exited)" if roomy else f" {churn}"
        interval = snapshot.sample_interval_ms if snapshot else self.detector.sample_interval_ms
        target = Path(self.target_path).name
        root_name = self._process_name(snapshot)
        if roomy and root_name.lower() != target.lower():
            target = f"{target} via {root_name}"
        pairs = [
            ("TARGET", target),
            ("PID", f"{pid} (attached)" if self.attached else str(pid)),
            ("PROCESSES" if roomy else "PROCS", processes),
            ("UPTIME", self._format_duration(self._uptime())),
        ]
        remaining = self.time_remaining
        if remaining is not None:
            pairs.append(("ENDS IN", self._format_duration(remaining)))
        pairs.append(("SAMPLES", f"{self._sample_count:,} @ {interval} ms"))
        info = self._label_values(*pairs)
        path = Text.assemble((" PATH ", f"bold {palette.muted}"), (self.target_path, palette.muted))
        path.no_wrap = True
        path.overflow = "ellipsis"
        return Group(bar, info, path)

    def _label_values(self, *pairs: tuple[str, str]) -> Text:
        palette = self.palette
        text = Text(no_wrap=True, overflow="ellipsis")
        for label, value in pairs:
            text.append(f" {label} ", style=f"bold {palette.muted}")
            text.append(value, style="bold")
            text.append("  ")
        return text

    def _render_footer(self) -> RenderableType:
        palette = self.palette
        roomy = self.console.size.width >= 120
        keys = Text(no_wrap=True, overflow="ellipsis")
        quit_label = ("detach & quit" if roomy else "detach") if self.attached else "quit"
        if self._view != "overview" and not self._show_help:
            # The view's own keys first; the target controls stay in help.
            controls = list(getattr(self, f"_{self._view}_controls")())
            controls += [
                ("ESC", "overview" if roomy else "back", palette.muted),
                ("?", "help", palette.muted),
                ("Q", quit_label, palette.accent_alt),
            ]
            for key, label, color in controls:
                keys.append(f" {key} ", style=f"bold {palette.ink} on {color}")
                keys.append(f" {label}  ", style=palette.muted)
            return keys
        controls = []
        if not self.manager.contains_monitor:
            controls += [
                ("SPACE", "pause/resume" if roomy else "pause", palette.accent),
                ("K", "kill tree" if roomy else "kill", palette.danger),
            ]
        controls.append(("E", "export report" if roomy else "export", palette.warn))
        if self._last_html_report is not None:
            controls.append(("O", "open report" if roomy else "open", palette.teal))
        controls.append(("S", "sort", palette.cyan))
        controls.append((f"{min(VIEWS)}-{max(VIEWS)}", "views", palette.cyan))
        controls.append(("?", "help", palette.muted))
        controls.append(("Q", quit_label, palette.accent_alt))
        for key, label, color in controls:
            keys.append(f" {key} ", style=f"bold {palette.ink} on {color}")
            keys.append(f" {label}  ", style=palette.muted)

        if self._last_report_id is not None:
            note = Text.assemble(
                ("last report ", palette.muted), (self._last_report_id, f"bold {palette.teal}")
            )
        else:
            note = Text("no report exported yet ", style=palette.muted)
        note.no_wrap = True
        note.overflow = "ellipsis"

        footer = Table.grid(expand=True)
        footer.add_column(no_wrap=True)
        footer.add_column(justify="right", ratio=1, no_wrap=True)
        footer.add_row(keys, note)
        return footer

    # ------------------------------------------------------------------
    # Metric cards
    # ------------------------------------------------------------------

    def _metric_card(
        self,
        title: str,
        color: str,
        value: str,
        aside: Text,
        spark: RenderableType,
        detail: str,
    ) -> Panel:
        headline = Table.grid(expand=True)
        headline.add_column(min_width=len(value), no_wrap=True)
        headline.add_column(justify="right", ratio=1, no_wrap=True, overflow="ellipsis")
        headline.add_row(Text(value, style=f"bold {color}"), aside)
        footer = Text(detail, style=self.palette.muted, no_wrap=True, overflow="ellipsis")
        return Panel(
            Group(headline, spark, footer),
            title=Text(f" {title} ", style=f"bold {color}"),
            title_align="left",
            border_style=self.palette.border,
            box=box.ROUNDED,
            padding=(0, 1),
        )

    def _render_cpu_card(self) -> Panel:
        totals = self._totals()
        palette, glyphs = self.palette, self.glyphs
        aside = Text(f"peak {self._peaks['cpu_percent']:.1f}%", style=palette.muted)
        detail = (
            f"{self._cpu_cores} cores {glyphs.sep} "
            f"read {self._format_bytes(totals.get('io_read_bytes_per_sec', 0.0))}/s "
            f"{glyphs.sep} write {self._format_bytes(totals.get('io_write_bytes_per_sec', 0.0))}/s"
        )
        return self._metric_card(
            "CPU",
            palette.cyan,
            f"{totals.get('cpu_percent', 0.0):.1f}%",
            aside,
            self._spark("cpu_percent", palette.cyan, floor=0.0, min_span=10.0),
            detail,
        )

    def _render_memory_card(self) -> Panel:
        """Chart private bytes, the memory signal's input, with RSS as context.

        Charting RSS here would show a trend the detector never judged, so
        the headline, delta, and chart follow private bytes and the working
        set is only listed in the footer.
        """
        totals = self._totals()
        palette, glyphs = self.palette, self.glyphs
        private = totals.get("private_bytes", 0.0)
        detail = (
            f"RSS (working set) {self._format_bytes(totals.get('rss_bytes', 0.0))} "
            f"{glyphs.sep} virtual {self._format_bytes(totals.get('vms_bytes', 0.0))}"
        )
        return self._metric_card(
            "MEMORY  PRIVATE",
            palette.accent,
            self._format_bytes(private),
            self._private_delta(),
            self._private_spark(),
            detail,
        )

    def _private_delta(self) -> Text:
        private = self._totals().get("private_bytes", 0.0)
        return self._delta_text(
            "private_bytes", self._format_bytes, steady=max(64 * 1024, private * 0.005)
        )

    def _private_spark(self) -> Sparkline:
        return self._spark(
            "private_bytes",
            self.palette.accent,
            min_span=max(1024 * 1024, self._peaks["private_bytes"] * 0.02),
        )

    def _render_handles_card(self) -> Panel:
        totals = self._totals()
        palette, glyphs = self.palette, self.glyphs
        handles = totals.get("handles", 0.0)
        processes = max(1, len(self.snapshot.processes) if self.snapshot else 1)
        breakdown = self._handle_breakdown()
        if breakdown:
            detail = breakdown
        else:
            detail = (
                f"peak {int(self._peaks['handles']):,} {glyphs.sep} "
                f"{handles / processes:,.0f} per process"
            )
        return self._metric_card(
            "HANDLES",
            palette.warn,
            f"{int(handles):,}",
            self._delta_text("handles", lambda value: f"{value:,.0f}", steady=0.5),
            self._spark("handles", palette.warn, min_span=8.0),
            detail,
        )

    def _handle_breakdown(self, limit: int = 4) -> str:
        """Largest named handle categories first, ``other`` last.

        For example ``file 120 · event 80 · registry 12 · other 40``.
        """
        counts = self._handle_types
        if not counts:
            return ""
        ranked = sorted(
            ((count, category) for category, count in counts.items() if count > 0),
            key=lambda item: (item[1] == "other", -item[0]),
        )
        shown = ranked[:limit]
        text = f" {self.glyphs.sep} ".join(f"{category} {count:,}" for count, category in shown)
        hidden = len(ranked) - len(shown)
        return f"{text} {self.glyphs.sep} +{hidden} more" if hidden > 0 else text

    def _render_threads_card(self) -> Panel:
        totals = self._totals()
        palette, glyphs = self.palette, self.glyphs
        processes = len(self.snapshot.processes) if self.snapshot else 0
        detail = (
            f"peak {int(self._peaks['threads']):,} {glyphs.sep} "
            f"across {processes} process{'es' if processes != 1 else ''}"
        )
        return self._metric_card(
            "THREADS",
            palette.teal,
            f"{int(totals.get('threads', 0.0)):,}",
            self._delta_text("threads", lambda value: f"{value:,.0f}", steady=0.5),
            self._spark("threads", palette.teal, min_span=4.0),
            detail,
        )

    def _render_metrics_strip(self) -> Panel:
        """One line per metric for terminals too short for the card grid."""
        palette = self.palette
        totals = self._totals()
        table = Table.grid(expand=True, padding=(0, 1))
        table.add_column(min_width=7, no_wrap=True)
        table.add_column(min_width=9, justify="right", no_wrap=True)
        table.add_column(min_width=12, no_wrap=True, overflow="ellipsis")
        table.add_column(ratio=1, no_wrap=True)
        self._spark_rows = 1
        count = lambda value: f"{value:,.0f}"  # noqa: E731
        rows = (
            ("CPU", palette.cyan, f"{totals.get('cpu_percent', 0.0):.1f}%",
             Text(f"peak {self._peaks['cpu_percent']:.0f}%", style=palette.muted),
             self._spark("cpu_percent", palette.cyan, floor=0.0, min_span=10.0)),
            # Private bytes drive the memory signal; the strip has no room for RSS.
            ("PRIVATE", palette.accent, self._format_bytes(totals.get("private_bytes", 0.0)),
             self._private_delta(), self._private_spark()),
            ("HANDLES", palette.warn, f"{int(totals.get('handles', 0.0)):,}",
             self._delta_text("handles", count, steady=0.5),
             self._spark("handles", palette.warn, min_span=8.0)),
            ("THREADS", palette.teal, f"{int(totals.get('threads', 0.0)):,}",
             self._delta_text("threads", count, steady=0.5),
             self._spark("threads", palette.teal, min_span=4.0)),
        )
        for label, color, value, aside, spark in rows:
            table.add_row(
                Text(label, style=f"bold {color}"),
                Text(value, style="bold"),
                aside,
                spark,
            )
        return Panel(
            table,
            title=Text(" METRICS ", style=f"bold {palette.accent}"),
            title_align="left",
            border_style=palette.border,
            box=box.ROUNDED,
            padding=(0, 1),
        )

    def _spark(
        self, key: str, color: str, floor: float | None = None, min_span: float = 0.0
    ) -> Sparkline:
        return Sparkline(
            [value for _stamp, value in self._history[key]],
            self.glyphs,
            color,
            height=self._spark_rows,
            floor=floor,
            min_span=min_span,
            empty_style=self.palette.muted,
        )

    def _delta_text(self, key: str, formatter, steady: float) -> Text:
        palette, glyphs = self.palette, self.glyphs
        delta = self._window_delta(key)
        window = f"{self.DELTA_WINDOW_SECONDS:.0f}s"
        if delta is None:
            return Text(f"{glyphs.flat} warming up", style=palette.muted)
        if abs(delta) < steady:
            return Text(f"{glyphs.flat} steady {window}", style=palette.muted)
        if delta > 0:
            return Text(f"{glyphs.up} +{formatter(delta)} {window}", style=f"bold {palette.warn}")
        return Text(f"{glyphs.down} -{formatter(-delta)} {window}", style=f"bold {palette.ok}")

    def _window_delta(self, key: str) -> float | None:
        series = self._history[key]
        if len(series) < 2:
            return None
        latest_stamp, latest_value = series[-1]
        cutoff = latest_stamp - self.DELTA_WINDOW_SECONDS
        for stamp, value in series:
            if stamp >= cutoff:
                return latest_value - value
        return 0.0

    # ------------------------------------------------------------------
    # Process tree
    # ------------------------------------------------------------------

    def _render_process_panel(self) -> Panel:
        snapshot = self.snapshot
        count = len(snapshot.processes) if snapshot else 0
        if self._sort_mode == "tree":
            title = f" PROCESS TREE  {count} "
        else:
            title = f" PROCESSES  {count}  {self.glyphs.sep} by {self._sort_label()} "
        return Panel(
            Sized(self._process_table),
            title=Text(title, style=f"bold {self.palette.accent_alt}"),
            title_align="left",
            border_style=self.palette.border,
            box=box.ROUNDED,
            padding=(0, 1),
        )

    def _process_table(self, width: int, height: int | None) -> RenderableType:
        palette, glyphs = self.palette, self.glyphs
        snapshot = self.snapshot
        if snapshot is None or not snapshot.processes:
            return Text("No live processes in the monitored tree", style=palette.muted)

        # Drop lower-priority columns before names collapse. Private bytes
        # drive the memory signal and the memory sort, so RSS goes first.
        if width >= 72:
            columns = ("cpu", "private", "rss", "handles", "threads", "status")
        elif width >= 50:
            columns = ("cpu", "private", "handles", "threads")
        else:
            columns = ("private",)
        headers = {
            "cpu": "CPU",
            "rss": "RSS",
            "private": "PRIVATE",
            "handles": "HNDL",
            "threads": "THR",
            "status": "STATUS",
        }
        table = Table(
            box=box.SIMPLE_HEAD,
            expand=True,
            show_edge=False,
            pad_edge=False,
            padding=(0, 1),
            header_style=f"bold {palette.muted}",
            border_style=palette.border,
        )
        table.add_column("PID", justify="right", no_wrap=True)
        table.add_column("PROCESS", ratio=1, min_width=10, no_wrap=True, overflow="ellipsis")
        for column in columns:
            justify = "left" if column == "status" else "right"
            table.add_column(headers[column], justify=justify, no_wrap=True)

        ordered = self._ordered_processes(snapshot)
        capacity = max(1, (height or len(ordered) + 2) - 2)
        shown = ordered if len(ordered) <= capacity else ordered[: capacity - 1]

        for proc, connector in shown:
            is_root = proc.pid == snapshot.root_pid
            marker = glyphs.brand if is_root else glyphs.info
            name = Text.assemble(
                (connector, palette.border),
                (f"{marker} ", palette.accent if is_root else palette.muted),
                (proc.name, "bold" if is_root else ""),
            )
            if proc.access_denied:
                cells: list[RenderableType] = [
                    Text("denied", style=palette.danger) if column == "status" else "n/a"
                    for column in columns
                ]
                table.add_row(str(proc.pid), name, *cells, style=palette.muted)
                continue
            values: dict[str, RenderableType] = {
                "cpu": f"{proc.cpu_percent:.1f}%",
                "rss": self._format_bytes(proc.rss_bytes),
                "private": self._format_bytes(proc.private_bytes),
                "handles": f"{proc.handles:,}",
                "threads": f"{proc.threads:,}",
                "status": Text(proc.status, style=self._process_status_style(proc.status)),
            }
            table.add_row(str(proc.pid), name, *(values[column] for column in columns))

        hidden = len(ordered) - len(shown)
        if hidden > 0:
            more = Text(f"{glyphs.ellipsis} {hidden} more", style=palette.muted)
            table.add_row("", more, *([""] * (len(table.columns) - 2)))
        return table

    def _sort_label(self) -> str:
        """Name the sort key; memory sorting ranks private bytes, not RSS."""
        return "private bytes" if self._sort_mode == "memory" else self._sort_mode

    def _ordered_processes(self, snapshot: MetricsSnapshot) -> list[tuple[ProcessMetrics, str]]:
        """Processes in tree order, or flat by the chosen metric, largest first."""
        if self._sort_mode == "tree":
            return self._tree_order(snapshot)
        metric = {
            "memory": lambda proc: proc.private_bytes,
            "handles": lambda proc: proc.handles,
            "cpu": lambda proc: proc.cpu_percent,
        }[self._sort_mode]
        ranked = sorted(snapshot.processes, key=lambda proc: (-metric(proc), proc.pid))
        return [(proc, "") for proc in ranked]

    def _tree_order(self, snapshot: MetricsSnapshot) -> list[tuple[ProcessMetrics, str]]:
        """Return processes depth-first with box-drawing connector prefixes."""
        glyphs = self.glyphs
        by_pid = {proc.pid: proc for proc in snapshot.processes}
        children: dict[int, list[ProcessMetrics]] = {}
        roots: list[ProcessMetrics] = []
        for proc in snapshot.processes:
            parent = proc.ppid
            # The root stays a root even if a descendant reused its parent's PID.
            if (
                proc.pid != snapshot.root_pid
                and parent is not None
                and parent != proc.pid
                and parent in by_pid
            ):
                children.setdefault(parent, []).append(proc)
            else:
                # The root, or an orphan whose parent already exited.
                roots.append(proc)
        roots.sort(key=lambda proc: (proc.pid != snapshot.root_pid, proc.pid))
        for siblings in children.values():
            siblings.sort(key=lambda proc: proc.pid)

        ordered: list[tuple[ProcessMetrics, str]] = []
        visited: set[int] = set()
        stack: list[tuple[ProcessMetrics, str, str]] = [
            (proc, "", "") for proc in reversed(roots)
        ]
        while stack:
            proc, connector, prefix = stack.pop()
            if proc.pid in visited:
                continue
            visited.add(proc.pid)
            ordered.append((proc, connector))
            kids = children.get(proc.pid, [])
            for index in reversed(range(len(kids))):
                last = index == len(kids) - 1
                branch = glyphs.tree_last if last else glyphs.tree_mid
                below = " " * len(glyphs.tree_last) if last else glyphs.tree_pipe
                stack.append((kids[index], prefix + branch, prefix + below))
        # Parent links that loop (reused PIDs) reach no root; list them flat.
        ordered += [(proc, "") for proc in snapshot.processes if proc.pid not in visited]
        return ordered

    def _process_status_style(self, status: str) -> str:
        palette = self.palette
        if status in {"running", "sleeping", "disk-sleep", "waking"}:
            return palette.ok
        if status in {"stopped", "idle", "parked"}:
            return palette.warn
        if status in {"dead", "zombie"}:
            return palette.danger
        return palette.muted

    # ------------------------------------------------------------------
    # Leak health and event log
    # ------------------------------------------------------------------

    def _render_health_panel(self) -> Panel:
        snapshot = self.snapshot
        severity = snapshot.severity if snapshot else LeakSeverity.NONE
        color = self._severity_color(severity)
        return Panel(
            Sized(self._health_body),
            title=Text(" LEAK HEALTH ", style=f"bold {color}"),
            title_align="left",
            border_style=self.palette.border if severity == LeakSeverity.NONE else color,
            box=box.ROUNDED,
            padding=(0, 1),
        )

    def _health_body(self, width: int, height: int | None) -> RenderableType:
        palette, glyphs = self.palette, self.glyphs
        snapshot = self.snapshot
        severity = snapshot.severity if snapshot else LeakSeverity.NONE
        color = self._severity_color(severity)
        warming = snapshot is not None and snapshot.warming_up

        if warming:
            banner = Text(
                f" {glyphs.dot} WARMING UP {glyphs.sep} detection in "
                f"{self._countdown(self.detector.warmup_remaining)} ",
                style=f"bold {palette.ink} on {palette.accent}",
            )
        elif severity == LeakSeverity.NONE:
            banner = self._evidence_banner(snapshot)
        else:
            banner = Text(
                f" {glyphs.warn} {severity.value.upper()} LEAK SIGNAL ",
                style=f"bold {palette.ink} on {color}",
            )
        # The banner is the current sample; this is the whole session so far.
        if self._worst_severity != LeakSeverity.NONE:
            worst_label, worst_color = self._worst_severity.value.upper(), self._severity_color(
                self._worst_severity
            )
        elif self.conclusive:
            worst_label, worst_color = "NONE", palette.ok
        else:
            worst_label, worst_color = "NOT JUDGED", palette.muted
        worst = Text.assemble(
            ("session worst ", palette.muted), (worst_label, f"bold {worst_color}")
        )
        top = Table.grid(expand=True)
        top.add_column(no_wrap=True)
        worst_beside = width >= banner.cell_len + worst.cell_len + 2
        if worst_beside:
            top.add_column(justify="right", ratio=1, no_wrap=True)
            top.add_row(banner, worst)
        else:
            top.add_row(banner)

        show_meter = width >= 44
        alerts = {alert.resource: alert for alert in (snapshot.alerts if snapshot else ())}
        rows = Table.grid(expand=True, padding=(0, 1))
        rows.add_column(min_width=8, no_wrap=True)
        if show_meter:
            rows.add_column(min_width=4, no_wrap=True)
        rows.add_column(min_width=8, no_wrap=True)
        rows.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        for resource in (LeakResource.MEMORY, LeakResource.HANDLES, LeakResource.THREADS):
            evidence = snapshot.evidence_for(resource) if snapshot else None
            name, meter, label, detail = self._health_row(
                resource, alerts.get(resource), warming, evidence
            )
            if show_meter:
                rows.add_row(name, meter, label, detail)
            else:
                rows.add_row(name, label, detail)

        windows = "/".join(format_duration(window) for window in self.detector.window_seconds)
        profile = f"profile {self.settings.profile} {glyphs.sep} " if self.settings.profile else ""
        footnote = Text(no_wrap=True, overflow="ellipsis")
        if not worst_beside:
            # Keep the session-wide result visible without taking another line.
            footnote.append_text(worst)
            footnote.append(f" {glyphs.sep} ", style=palette.muted)
        coverage = self._coverage_note(snapshot)
        if coverage:
            # Coverage comes first so a narrow panel truncates the settings instead.
            footnote.append(coverage, style=f"bold {palette.warn}")
            footnote.append(f" {glyphs.sep} ", style=palette.muted)
        footnote.append(
            f"{profile}windows {windows} {glyphs.sep} every {self.detector.sample_interval_ms} ms",
            style=palette.muted,
        )
        freshness = self._freshness_line()
        if self._health_spacious:
            return Group(top, Text(""), rows, freshness or Text(""), footnote)
        if freshness is not None:
            footnote.append(f" {glyphs.sep} ", style=palette.muted)
            footnote.append_text(freshness)
        return Group(top, rows, footnote)

    def _freshness_line(self) -> Text | None:
        """Sample age, cost and cadence; warn-coloured when samples are stale.

        Stale means the last good sample is older than three requested
        intervals, which happens when sampling fails or the monitor stalls.
        """
        text = self.freshness_text()
        if text is None or self.snapshot is None:
            return None
        palette = self.palette
        age = time.time() - self.snapshot.timestamp
        stale = not self.snapshot.monitoring_paused and age > 3 * self.detector.sample_interval_ms / 1000
        return Text(text, style=f"bold {palette.warn}" if stale else palette.muted,
                    no_wrap=True, overflow="ellipsis")

    def _evidence_banner(self, snapshot: MetricsSnapshot | None) -> Text:
        """The banner when no signal is active: say how much has been judged.

        "No signal" only means "no growth" once a window was evaluated, so
        collecting, unavailable, and paused evidence each get their own text.
        """
        palette, glyphs = self.palette, self.glyphs
        evidence = snapshot.evidence if snapshot else ()
        states = {item.state for item in evidence}

        def banner(text: str, color: str) -> Text:
            return Text(f" {text} ", style=f"bold {palette.ink} on {color}")

        if not evidence:
            return banner(f"{glyphs.dot} AWAITING SAMPLES", palette.accent)
        if states == {EvidenceState.UNAVAILABLE}:
            return banner(
                f"{glyphs.error} UNAVAILABLE {glyphs.sep} {unavailable_reason(evidence)}",
                palette.danger,
            )
        if EvidenceState.STALE in states:
            return banner(f"{glyphs.dot} PAUSED {glyphs.sep} evidence on hold", palette.warn)
        collecting = [item for item in evidence if item.state == EvidenceState.COLLECTING]
        if collecting:
            least = min(collecting, key=lambda item: item.elapsed_seconds)
            return banner(
                f"{glyphs.dot} COLLECTING EVIDENCE {format_duration(least.elapsed_seconds)}"
                f"/{format_duration(least.needed_seconds)}",
                palette.accent,
            )
        judged = [
            item.evaluated_window_seconds
            for item in evidence
            if item.state == EvidenceState.NO_GROWTH and item.evaluated_window_seconds
        ]
        if not judged:
            return banner(f"{glyphs.dot} AWAITING SAMPLES", palette.accent)
        return banner(
            f"{glyphs.ok} NO SUSTAINED GROWTH ({format_duration(min(judged))} window)", palette.ok
        )

    def _coverage_note(self, snapshot: MetricsSnapshot | None) -> str:
        """``PARTIAL: 1 process unreadable`` when the totals leave processes out."""
        if snapshot is None or not snapshot.coverage:
            return ""
        unreadable = len(snapshot.unmeasured_pids)
        if not unreadable:
            return ""
        live = max(item.total for item in snapshot.coverage.values())
        noun = "process" if unreadable == 1 else "processes"
        if unreadable >= live:
            return f"UNAVAILABLE: {unreadable} {noun} unreadable"
        return f"PARTIAL: {unreadable} {noun} unreadable"

    def _health_row(
        self,
        resource: LeakResource,
        alert: LeakAlert | None,
        warming: bool = False,
        evidence: ResourceEvidence | None = None,
    ) -> tuple[RenderableType, ...]:
        palette, glyphs = self.palette, self.glyphs
        severity = alert.severity if alert else LeakSeverity.NONE
        color = self._severity_color(severity)
        level = severity_rank(severity)
        meter = Text.assemble(
            (glyphs.meter_full * level, color),
            (glyphs.meter_empty * (4 - level), palette.border),
        )
        name = Text(resource.value.upper(), style=f"bold {palette.muted}")
        if warming:
            label = Text("WAIT", style=f"bold {palette.accent}")
            return name, meter, label, Text("sampling during warm-up", style=palette.muted)
        if alert is not None:
            label = Text(severity.value.upper(), style=f"bold {color}")
            return name, meter, label, Text(self._health_detail(alert), style=color)
        label_text, label_color, detail = self._evidence_row(evidence)
        if evidence is not None and evidence.partial and evidence.coverage is not None:
            # First, so a narrow panel cuts the explanation rather than the caveat.
            covered = f"partial {evidence.coverage.measured}/{evidence.coverage.total}"
            detail = f"{covered} {glyphs.sep} {detail}"
        return (
            name,
            meter,
            Text(label_text, style=f"bold {label_color}"),
            Text(detail, style=palette.muted),
        )

    def _evidence_row(self, evidence: ResourceEvidence | None) -> tuple[str, str, str]:
        """Label, label colour, and detail for a resource without an active signal."""
        palette, sep = self.palette, self.glyphs.sep
        if evidence is None:
            return "-", palette.muted, "no evidence yet"
        state = evidence.state
        if state == EvidenceState.NO_GROWTH:
            window = format_duration(evidence.evaluated_window_seconds or 0.0)
            return "STEADY", palette.ok, f"no sustained growth in {window} window"
        if state == EvidenceState.COLLECTING:
            # Once enough samples exist only the elapsed time is still short.
            samples = (
                f"{evidence.valid_samples} samples"
                if evidence.valid_samples >= evidence.needed_samples
                else f"{evidence.valid_samples}/{evidence.needed_samples} samples"
            )
            return (
                "COLLECT",
                palette.accent,
                f"{samples} {sep} {format_duration(evidence.elapsed_seconds)}/"
                f"{format_duration(evidence.needed_seconds)}",
            )
        if state == EvidenceState.UNAVAILABLE:
            return "N/A", palette.danger, evidence.unavailable_reason
        if state == EvidenceState.STALE:
            return "PAUSED", palette.warn, "evidence on hold while paused"
        if state == EvidenceState.WARMING_UP:
            return "WAIT", palette.accent, "sampling during warm-up"
        return "-", palette.muted, "no evidence yet"

    def _health_detail(self, alert: LeakAlert) -> str:
        """Compact alert summary that keeps the growing handle type visible.

        The panel is narrow, so the type that grew comes right after the
        amount, ahead of the rate: ``+358 in 10s · file +340 · 39.8/s``.
        The process the detector ranked first joins the amount:
        ``+40.0 MB private in 10s, mostly worker.exe PID 1234 · 4.0 MB/s``.
        """
        delta = alert.current_value - alert.baseline_value
        if alert.resource == LeakResource.MEMORY:
            # Say which counter grew: the memory card also lists RSS.
            amount = f"{self._format_bytes(delta)} private"
            rate = f"{self._format_bytes(alert.slope_per_second)}/s"
        else:
            amount = f"{delta:,.0f}"
            rate = f"{alert.slope_per_second:,.1f}/s"
        parts = [f"+{amount} in {alert.window_seconds:.0f}s"]
        attribution = self.attribution_text(alert)
        if attribution:
            # Who grew matters more than how fast, so it rides on the amount.
            parts[0] += f", {attribution}"
        growth = describe_type_growth(alert.growth_by_type or {}, limit=1)
        if growth:
            parts.append(growth.removeprefix("by type: "))
        parts.append(rate)
        return f" {self.glyphs.sep} ".join(parts)

    def _render_event_panel(self) -> Panel:
        return Panel(
            Sized(self._event_table),
            title=Text(" EVENT LOG ", style=f"bold {self.palette.cyan}"),
            title_align="left",
            border_style=self.palette.border,
            box=box.ROUNDED,
            padding=(0, 1),
        )

    def _event_table(self, width: int, height: int | None) -> RenderableType:
        """Newest events that fit, each wrapped to at most ``EVENT_LINES`` lines."""
        palette, glyphs = self.palette, self.glyphs
        if not self._events:
            return Text("Waiting for telemetry events...", style=palette.muted)

        # Time (8 cells), a gap, the icon, and another gap precede the message.
        message_width = max(8, width - 11)
        budget = height if height else len(self._events) * self.EVENT_LINES
        rows: list[tuple[float, str, Text]] = []
        for stamp, message, level in reversed(self._events):
            if budget <= 0:
                break
            icon, color = self._event_icon(level)
            styled = Text(message, style=color if level in {"error", "alert"} else "")
            lines = list(styled.wrap(self.console, message_width))
            limit = min(self.EVENT_LINES, budget)
            if len(lines) > limit:
                lines = lines[:limit]
                last = lines[-1]
                last.truncate(max(1, message_width - len(glyphs.ellipsis)))
                last.append(glyphs.ellipsis)
            budget -= len(lines)
            rows.append((stamp, level, Text("\n").join(lines)))

        table = Table.grid(expand=True, padding=(0, 1))
        table.add_column(width=8, no_wrap=True, style=palette.muted)
        table.add_column(width=1, no_wrap=True)
        table.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        for stamp, level, message in reversed(rows):
            icon, color = self._event_icon(level)
            table.add_row(
                time.strftime("%H:%M:%S", time.localtime(stamp)),
                Text(icon, style=f"bold {color}"),
                message,
            )
        return table

    def _event_icon(self, level: str) -> tuple[str, str]:
        palette, glyphs = self.palette, self.glyphs
        return {
            "ok": (glyphs.ok, palette.ok),
            "warn": (glyphs.warn, palette.warn),
            "alert": (glyphs.warn, palette.alert),
            "error": (glyphs.error, palette.danger),
        }.get(level, (glyphs.info, palette.accent))

    # ------------------------------------------------------------------
    # Session summary
    # ------------------------------------------------------------------

    def render_summary(self) -> Panel:
        """Build the panel printed after the live dashboard closes."""
        palette, glyphs = self.palette, self.glyphs
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style=f"bold {palette.muted}", no_wrap=True)
        # Fold rather than truncate: report paths are meant to be copied.
        grid.add_column(overflow="fold")
        grid.add_row(
            "Target",
            Text(
                f"{Path(self.target_path).name} {glyphs.sep} PID {self.root_pid or '-'} "
                f"{glyphs.sep} {self.outcome_text()}"
            ),
        )
        duration = f"{self._format_duration(self._uptime())} {glyphs.sep} {self._sample_count:,} samples"
        thinning = self.chart_thinning_text()
        if thinning:
            duration += f" {glyphs.sep} {thinning}"
        grid.add_row("Duration", duration)
        warmup = self.warmup_text()
        if warmup:
            grid.add_row("Warm-up", warmup)
        monitor = self.monitor_cost_text()
        if monitor:
            grid.add_row("Monitor", monitor)
        if self.settings.source is not None:
            grid.add_row("Settings", Text(self.settings.describe()))
        grid.add_row(
            "Peaks",
            f"private {self._format_bytes(self._peaks['private_bytes'])} {glyphs.sep} "
            f"RSS (working set) {self._format_bytes(self._peaks['rss_bytes'])} {glyphs.sep} "
            f"{int(self._peaks['handles']):,} handles {glyphs.sep} "
            f"{int(self._peaks['threads']):,} threads",
        )
        grid.add_row("Signals", Text(self.signals_text()))
        worst = self._worst_severity
        if worst != LeakSeverity.NONE:
            verdict = Text(
                f"{worst.value.upper()} leak signal observed",
                style=f"bold {self._severity_color(worst)}",
            )
        elif self.conclusive:
            verdict = Text("no leak signals observed", style=f"bold {palette.ok}")
        else:
            verdict = Text(f"inconclusive: {self.inconclusive_reason()}", style=f"bold {palette.warn}")
        coverage = self.coverage_text()
        if coverage:
            # A clean result on partial totals may miss growth in what was unreadable.
            verdict.append(f" (partial coverage: {coverage})", style=f"bold {palette.warn}")
        grid.add_row("Verdict", verdict)
        if self._last_report:
            report = Text.assemble(
                (self._last_report_id or "", f"bold {palette.teal}"),
                (f"\n{self._last_report}", palette.muted),
            )
            if self._last_html_report is not None:
                report.append(f"\n{self._last_html_report}", style=palette.muted)
        else:
            report = Text("not exported (press E during a session)", style=palette.muted)
        grid.add_row("Report", report)
        return Panel(
            grid,
            title=Text(f" {glyphs.brand} {self.TITLE} SESSION SUMMARY ", style=f"bold {palette.accent}"),
            title_align="left",
            border_style=palette.border,
            box=box.ROUNDED,
            padding=(0, 1),
            expand=False,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _open_last_report(self) -> None:
        """Open the latest HTML report in the default browser."""
        path = self._last_html_report
        if path is None:
            self._append_event("No HTML report yet; press E to export one", "warn")
            return
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            elif not webbrowser.open(path.resolve().as_uri()):
                raise OSError("no browser is available")
        except OSError as exc:
            self._append_event(f"Could not open {path.name}: {exc}", "error")
        else:
            self._append_event(f"Opened {path.name}", "info")

    def _status(self, snapshot: MetricsSnapshot | None) -> tuple[str, str]:
        palette = self.palette
        if snapshot is None:
            return "STARTING", palette.accent
        if not snapshot.process_alive:
            return "EXITED", palette.danger
        if self.detector.is_paused or self.manager.is_paused:
            return "PAUSED", palette.warn
        if snapshot.warming_up:
            return "WARMING UP", palette.accent
        return "RUNNING", palette.ok

    @staticmethod
    def _countdown(seconds: float) -> str:
        """Whole seconds left, rounded up, as ``7s`` or ``1m5s``."""
        return format_duration(max(1.0, math.ceil(seconds)))

    def _severity_color(self, severity: LeakSeverity) -> str:
        palette = self.palette
        return {
            LeakSeverity.NONE: palette.ok,
            LeakSeverity.LOW: palette.warn,
            LeakSeverity.MEDIUM: palette.alert,
            LeakSeverity.HIGH: palette.danger,
            LeakSeverity.CRITICAL: palette.critical,
        }[severity]

    def _read_key(self) -> str | None:
        """The next key as a character or a name such as ``up`` (see ``keys``)."""
        return self._keys.read()


__all__ = ["DashboardApp"]
