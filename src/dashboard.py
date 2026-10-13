"""Rich terminal dashboard for DrainTracer."""

from __future__ import annotations

import os
import select
import sys
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
    LeakAlert,
    LeakResource,
    LeakSeverity,
    MetricsSnapshot,
    ProcessMetrics,
    severity_rank,
)
from .config import MonitorSettings
from .report import DEFAULT_REPORT_DIR
from .session import MonitorSession
from .theme import PALETTE
from .units import format_duration
from .widgets import Sized, Sparkline


class DashboardApp(MonitorSession):
    """Render a monitor session live in the terminal and handle its controls."""

    VERSION = "v1.2"
    DELTA_WINDOW_SECONDS = 10.0
    FRAME_SECONDS = 0.1

    KILL_CONFIRM_SECONDS = 3.0

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
        self._kill_armed_until = 0.0
        self._cpu_cores = psutil.cpu_count(logical=True) or 1

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
            self.console.print(
                Panel(
                    Text.assemble(
                        ("Unable to start monitor\n\n", f"bold {palette.danger}"),
                        (str(exc), ""),
                    ),
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
                self._render(),
                console=self.console,
                auto_refresh=False,
                screen=self.console.is_terminal,
                transient=False,
            ) as live:
                while not self._quit_requested:
                    self.poll()
                    self._handle_key(self._read_key())
                    if self.timed_out and not self._quit_requested:
                        self._quit_requested = True
                        self._append_event(
                            f"Time limit of {format_duration(self.timeout or 0.0)} reached",
                            "info",
                        )
                    live.update(self._render(), refresh=True)
                    time.sleep(self.FRAME_SECONDS)
        except KeyboardInterrupt:
            self._append_event("Dashboard interrupted by user", "warn")
        finally:
            self.shutdown()

        self.console.print(self.render_summary())
        return 0

    def _handle_key(self, key: str | None) -> None:
        if not key:
            return
        normalized = key.lower()

        if key == " ":
            if self.detector.is_paused:
                if self.manager.resume():
                    self.detector.resume()
                    self._append_event("Monitoring resumed", "ok")
                else:
                    self._append_event(
                        f"Resume failed: {self.manager.last_error or 'unknown error'}",
                        "error",
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
            self._stopped_target = True
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
            except OSError as exc:
                self._append_event(f"Export failed: {exc}", "error")
            else:
                formats = "JSON + HTML" if self._last_html_report else "JSON"
                self._append_event(
                    f"Report {self._last_report_id} saved to {path.parent} ({formats})", "ok"
                )
        elif normalized == "o":
            self._open_last_report()
        elif normalized == "q":
            self._quit_requested = True
            self._append_event("Quit requested", "info")

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    WIDE_LAYOUT_COLUMNS = 110
    COMPACT_LAYOUT_ROWS = 30
    SPLIT_BOTTOM_COLUMNS = 80

    def _render(self) -> Layout:
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
        interval = snapshot.sample_interval_ms if snapshot else self.detector.sample_interval_ms
        target = Path(self.target_path).name
        root_name = self._process_name(snapshot)
        if roomy and root_name.lower() != target.lower():
            target = f"{target} via {root_name}"
        pairs = [
            ("TARGET", target),
            ("PID", f"{pid} (attached)" if self.attached else str(pid)),
            ("PROCESSES" if roomy else "PROCS", str(process_count)),
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
        keys = Text(no_wrap=True, overflow="ellipsis")
        controls = [
            ("SPACE", "pause/resume", palette.accent),
            ("K", "kill tree", palette.danger),
            ("E", "export report", palette.warn),
        ]
        if self._last_html_report is not None:
            controls.append(("O", "open report", palette.teal))
        controls.append(("Q", "detach & quit" if self.attached else "quit", palette.accent_alt))
        for key, label, color in controls:
            keys.append(f" {key} ", style=f"bold {palette.ink} on {color}")
            keys.append(f" {label}   ", style=palette.muted)

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
            f"{self._cpu_cores} logical cores {glyphs.sep} "
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
        totals = self._totals()
        palette, glyphs = self.palette, self.glyphs
        rss = totals.get("rss_bytes", 0.0)
        detail = (
            f"private {self._format_bytes(totals.get('private_bytes', 0.0))} "
            f"{glyphs.sep} virtual {self._format_bytes(totals.get('vms_bytes', 0.0))}"
        )
        return self._metric_card(
            "MEMORY  RSS",
            palette.accent,
            self._format_bytes(rss),
            self._delta_text("rss_bytes", self._format_bytes, steady=max(64 * 1024, rss * 0.005)),
            self._spark(
                "rss_bytes",
                palette.accent,
                min_span=max(1024 * 1024, self._peaks["rss_bytes"] * 0.02),
            ),
            detail,
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
            ("MEMORY", palette.accent, self._format_bytes(totals.get("rss_bytes", 0.0)),
             self._delta_text("rss_bytes", self._format_bytes,
                              steady=max(64 * 1024, totals.get("rss_bytes", 0.0) * 0.005)),
             self._spark("rss_bytes", palette.accent,
                         min_span=max(1024 * 1024, self._peaks["rss_bytes"] * 0.02))),
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
        return Panel(
            Sized(self._process_table),
            title=Text(f" PROCESS TREE  {count} ", style=f"bold {self.palette.accent_alt}"),
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

        # Drop lower-priority columns before names collapse.
        if width >= 72:
            columns = ("cpu", "rss", "private", "handles", "threads", "status")
        elif width >= 50:
            columns = ("cpu", "rss", "handles", "threads")
        else:
            columns = ("rss",)
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

        ordered = self._tree_order(snapshot)
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

    def _tree_order(self, snapshot: MetricsSnapshot) -> list[tuple[ProcessMetrics, str]]:
        """Return processes depth-first with box-drawing connector prefixes."""
        glyphs = self.glyphs
        by_pid = {proc.pid: proc for proc in snapshot.processes}
        children: dict[int, list[ProcessMetrics]] = {}
        roots: list[ProcessMetrics] = []
        for proc in snapshot.processes:
            parent = proc.ppid
            if parent is not None and parent != proc.pid and parent in by_pid:
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

        if severity == LeakSeverity.NONE:
            banner = Text(f" {glyphs.ok} NO ACTIVE LEAK SIGNALS ", style=f"bold {palette.ink} on {color}")
        else:
            banner = Text(
                f" {glyphs.warn} {severity.value.upper()} LEAK SIGNAL ",
                style=f"bold {palette.ink} on {color}",
            )
        worst = Text.assemble(
            ("session worst ", palette.muted),
            (self._worst_severity.value.upper(), f"bold {self._severity_color(self._worst_severity)}"),
        )
        top = Table.grid(expand=True)
        top.add_column(no_wrap=True)
        if width >= banner.cell_len + worst.cell_len + 2:
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
            name, meter, label, detail = self._health_row(resource, alerts.get(resource))
            if show_meter:
                rows.add_row(name, meter, label, detail)
            else:
                rows.add_row(name, label, detail)

        windows = "/".join(format_duration(window) for window in self.detector.window_seconds)
        profile = f"profile {self.settings.profile} {glyphs.sep} " if self.settings.profile else ""
        footnote = Text(
            f"{profile}windows {windows} {glyphs.sep} "
            f"sample every {self.detector.sample_interval_ms} ms",
            style=palette.muted,
            no_wrap=True,
            overflow="ellipsis",
        )
        if self._health_spacious:
            return Group(top, Text(""), rows, Text(""), footnote)
        return Group(top, rows, footnote)

    def _health_row(
        self, resource: LeakResource, alert: LeakAlert | None
    ) -> tuple[RenderableType, ...]:
        palette, glyphs = self.palette, self.glyphs
        severity = alert.severity if alert else LeakSeverity.NONE
        color = self._severity_color(severity)
        level = severity_rank(severity)
        meter = Text.assemble(
            (glyphs.meter_full * level, color),
            (glyphs.meter_empty * (4 - level), palette.border),
        )
        label = Text("CLEAR" if alert is None else severity.value.upper(), style=f"bold {color}")
        if alert is None:
            detail = Text("no sustained growth", style=palette.muted)
        else:
            detail = Text(self._alert_summary(alert), style=color)
        name = Text(resource.value.upper(), style=f"bold {palette.muted}")
        return name, meter, label, detail

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
        palette = self.palette
        capacity = max(1, height or len(self._events) or 1)
        events = list(self._events)[-capacity:]
        if not events:
            return Text("Waiting for telemetry events...", style=palette.muted)

        table = Table.grid(expand=True, padding=(0, 1))
        table.add_column(width=8, no_wrap=True, style=palette.muted)
        table.add_column(width=1, no_wrap=True)
        table.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        for stamp, message, level in events:
            icon, color = self._event_icon(level)
            table.add_row(
                time.strftime("%H:%M:%S", time.localtime(stamp)),
                Text(icon, style=f"bold {color}"),
                Text(message, style=color if level in {"error", "alert"} else ""),
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
        grid.add_column()
        grid.add_row("Target", f"{Path(self.target_path).name}")
        if self.attached:
            grid.add_row(
                "Mode",
                f"attached to PID {self.attach_pid} {glyphs.sep} "
                + ("left running" if self._left_running else "no longer running"),
            )
        grid.add_row(
            "Duration",
            f"{self._format_duration(self._uptime())} {glyphs.sep} {self._sample_count:,} samples",
        )
        grid.add_row(
            "Peaks",
            f"RSS {self._format_bytes(self._peaks['rss_bytes'])} {glyphs.sep} "
            f"{int(self._peaks['handles']):,} handles {glyphs.sep} "
            f"{int(self._peaks['threads']):,} threads",
        )
        worst_color = self._severity_color(self._worst_severity)
        verdict = (
            Text("no leak signals observed", style=f"bold {palette.ok}")
            if self._worst_severity == LeakSeverity.NONE
            else Text(
                f"{self._worst_severity.value.upper()} leak signal observed",
                style=f"bold {worst_color}",
            )
        )
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
        return "RUNNING", palette.ok

    def _severity_color(self, severity: LeakSeverity) -> str:
        palette = self.palette
        return {
            LeakSeverity.NONE: palette.ok,
            LeakSeverity.LOW: palette.warn,
            LeakSeverity.MEDIUM: palette.alert,
            LeakSeverity.HIGH: palette.danger,
            LeakSeverity.CRITICAL: palette.critical,
        }[severity]

    @staticmethod
    def _read_key() -> str | None:
        if os.name == "nt":
            import msvcrt

            if msvcrt.kbhit():
                key = msvcrt.getwch()
                if key in ("\x00", "\xe0") and msvcrt.kbhit():
                    msvcrt.getwch()
                    return None
                return key
            return None

        if not sys.stdin.isatty():
            return None
        ready, _, _ = select.select([sys.stdin], [], [], 0)
        return sys.stdin.read(1) if ready else None


__all__ = ["DashboardApp"]
