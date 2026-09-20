"""Rich terminal dashboard for DrainTracer."""

from __future__ import annotations

import json
import os
import select
import sys
import time
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Any

from rich import box
from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .events import LeakAlert, LeakSeverity, MetricsSnapshot
from .leak_detector import LeakDetector
from .process_runner import ProcessManager


class DashboardApp:
    """Own the monitor lifecycle and render live snapshots in the terminal."""

    TITLE = "DRAINTRACER"
    VERSION = "v0.1"
    HISTORY_LIMIT = 48

    def __init__(
        self,
        target_path: str,
        args: list[str] | None = None,
        console: Console | None = None,
        export_path: str = "leak_report.json",
    ) -> None:
        self.target_path = str(Path(target_path).resolve())
        self.console = console or Console()
        self.manager = ProcessManager(self.target_path, args=args)
        self.detector = LeakDetector(self.manager)
        self.export_path = Path(export_path)

        self.snapshot: MetricsSnapshot | None = None
        self._cpu_history: deque[float] = deque(maxlen=self.HISTORY_LIMIT)
        self._memory_history: deque[float] = deque(maxlen=self.HISTORY_LIMIT)
        self._handle_history: deque[float] = deque(maxlen=self.HISTORY_LIMIT)
        self._thread_history: deque[float] = deque(maxlen=self.HISTORY_LIMIT)
        self._snapshot_history: deque[dict[str, Any]] = deque(maxlen=600)
        self._events: deque[str] = deque(maxlen=8)
        self._last_severity = LeakSeverity.NONE
        self._quit_requested = False
        self._terminated_by_user = False

    def run(self) -> int:
        """Start monitoring and run the interactive dashboard until quit."""
        try:
            with self.console.status(
                "[bold cyan]ATTACHING MONITOR[/bold cyan] "
                "[dim]initializing process telemetry...[/dim]",
                spinner="dots",
            ):
                self.manager.start()
                self._accept_snapshot(self.detector.sample_once())
                self.detector.start()
                self._append_event(
                    f"Attached to {Path(self.target_path).name} | PID {self.manager.root_pid}"
                )
                time.sleep(0.2)
        except Exception as exc:
            if self.manager.has_tracked_processes:
                self.manager.kill()
            self.console.print(
                Panel(
                    f"[bold red]Unable to start monitor[/bold red]\n\n{exc}",
                    title="STARTUP ERROR",
                    border_style="red",
                    box=box.ASCII,
                )
            )
            return 1

        try:
            with Live(
                self._render(),
                console=self.console,
                refresh_per_second=8,
                screen=False,
                transient=False,
            ) as live:
                while not self._quit_requested:
                    self._consume_stream()
                    self._handle_key(self._read_key())
                    live.update(self._render(), refresh=True)
                    time.sleep(0.08)
        except KeyboardInterrupt:
            self._append_event("Dashboard interrupted by user")
        finally:
            self.detector.stop()
            if self.manager.is_running or self.manager.has_tracked_processes:
                self.manager.terminate()

        return 0

    def _consume_stream(self) -> None:
        snapshots = self.detector.stream.drain()
        if not snapshots:
            return
        self._accept_snapshot(snapshots[-1])

    def _accept_snapshot(self, snapshot: MetricsSnapshot) -> None:
        previous = self.snapshot
        self.snapshot = snapshot

        self._cpu_history.append(snapshot.totals.get("cpu_percent", 0.0))
        self._memory_history.append(snapshot.totals.get("rss_bytes", 0.0))
        self._handle_history.append(snapshot.totals.get("handles", 0.0))
        self._thread_history.append(snapshot.totals.get("threads", 0.0))
        self._snapshot_history.append(self._snapshot_to_dict(snapshot))

        if snapshot.severity != self._last_severity:
            if snapshot.severity == LeakSeverity.NONE:
                self._append_event("Leak indicators cleared | system healthy")
            else:
                self._append_event(
                    f"{snapshot.severity.value.upper()} leak signal detected"
                )
            self._last_severity = snapshot.severity

        if previous and snapshot.process_alive is False and previous.process_alive:
            self._append_event("Target process exited")

        for alert in snapshot.alerts:
            if previous is None or alert.message not in {
                prior.message for prior in previous.alerts
            }:
                self._append_event(alert.message)

    def _handle_key(self, key: str | None) -> None:
        if not key:
            return
        normalized = key.lower()

        if key == " ":
            if self.detector.is_paused:
                if self.manager.resume():
                    self.detector.resume()
                    self._append_event("Monitoring resumed")
                else:
                    self._append_event(
                        f"Resume failed: {self.manager.last_error or 'unknown error'}"
                    )
            else:
                if self.manager.pause():
                    self.detector.pause()
                    self._append_event("Monitoring paused")
                else:
                    self._append_event(
                        f"Pause failed: {self.manager.last_error or 'unknown error'}"
                    )
        elif normalized == "k":
            self.manager.kill()
            self._terminated_by_user = True
            if self.manager.has_tracked_processes:
                self._append_event(
                    f"Target cleanup incomplete: {self.manager.last_error or 'unknown error'}"
                )
            else:
                self._append_event("Target process tree terminated")
        elif normalized == "e":
            try:
                path = self.export_log()
            except OSError as exc:
                self._append_event(f"Export failed: {exc}")
            else:
                self._append_event(f"Session exported to {path}")
        elif normalized == "q":
            self._quit_requested = True
            self._append_event("Quit requested")

    def export_log(self) -> Path:
        """Write collected snapshots and event messages to a JSON report."""
        report = {
            "app": self.TITLE,
            "version": self.VERSION,
            "target_path": self.target_path,
            "root_pid": self.manager.root_pid,
            "exported_at": time.time(),
            "events": list(self._events),
            "snapshots": list(self._snapshot_history),
        }
        self.export_path.write_text(
            json.dumps(report, indent=2),
            encoding="utf-8",
        )
        return self.export_path

    def _render(self) -> Layout:
        layout = Layout(name="root")
        layout.split_column(
            Layout(self._render_header(), name="header", size=6),
            Layout(name="body", ratio=1),
            Layout(self._render_footer(), name="footer", size=3),
        )
        layout["body"].split_row(
            Layout(self._render_metrics_panel(), name="metrics", ratio=3),
            Layout(self._render_diagnostics_panel(), name="diagnostics", ratio=2),
        )
        return layout

    def _render_header(self) -> Panel:
        snapshot = self.snapshot
        status, status_style = self._status(snapshot)
        process_name = self._process_name(snapshot)
        pid = snapshot.root_pid if snapshot and snapshot.root_pid else "-"

        title = Text()
        title.append(f"{self.TITLE} ", style="bold cyan")
        title.append(self.VERSION, style="bold magenta")

        details = Table.grid(expand=True, padding=(0, 2))
        details.add_column(style="dim")
        details.add_column(style="white")
        details.add_column(style="dim")
        details.add_column(style="white")
        details.add_row("TARGET", process_name, "PID", str(pid))
        details.add_row(
            "PATH",
            self.target_path,
            "STATUS",
            Text(status, style=status_style),
        )

        return Panel(
            Group(title, Rule(characters="-", style="magenta"), details),
            border_style="magenta",
            padding=(0, 1),
            box=box.ASCII,
        )

    def _render_metrics_panel(self) -> Panel:
        totals = self._totals()
        cpu = totals["cpu_percent"]
        memory = totals["rss_bytes"]
        handles = totals["handles"]
        threads = totals["threads"]

        memory_text = Text()
        memory_text.append(self._format_bytes(memory), style="bold white")
        memory_text.append(f"  {self._trend(self._memory_history)}", style="bold")
        memory_text.append("  RSS / working set", style="dim")

        handle_text = Text()
        handle_text.append(f"{int(handles):,}", style="bold yellow")
        handle_text.append(f"  {self._trend(self._handle_history)}  handles", style="dim")

        thread_text = Text()
        thread_text.append(f"{int(threads):,}", style="bold green")
        thread_text.append("  active threads", style="dim")

        memory_panel = Panel(
            Group(Text("MEMORY USAGE", style="bold cyan"), memory_text),
            border_style="cyan",
            padding=(1, 2),
            box=box.ASCII,
        )
        handles_panel = Panel(
            Group(Text("SYSTEM HANDLES", style="bold cyan"), handle_text),
            border_style="cyan",
            padding=(1, 2),
            box=box.ASCII,
        )
        threads_panel = Panel(
            Group(Text("THREAD COUNT", style="bold cyan"), thread_text),
            border_style="cyan",
            padding=(1, 2),
            box=box.ASCII,
        )

        grid = Table.grid(expand=True)
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)
        grid.add_row(
            Panel(
                Group(
                    Text("CPU UTILIZATION", style="bold cyan"),
                    self._progress_bar(cpu),
                    self._sparkline(self._cpu_history, "cyan"),
                ),
                border_style="cyan",
                padding=(1, 2),
                box=box.ASCII,
            ),
            memory_panel,
        )
        grid.add_row(handles_panel, threads_panel)

        return Panel(
            Group(Text("RESOURCE TELEMETRY", style="bold white"), grid),
            title="[bold cyan] LIVE METRICS [/bold cyan]",
            border_style="cyan",
            padding=(0, 1),
            box=box.ASCII,
        )

    def _render_diagnostics_panel(self) -> Panel:
        snapshot = self.snapshot
        severity = snapshot.severity if snapshot else LeakSeverity.NONE
        style = self._severity_style(severity)

        if severity == LeakSeverity.NONE:
            banner = Panel(
                Align.center(Text("[OK]  NO ACTIVE LEAK SIGNALS", style="bold green")),
                border_style="green",
                padding=(1, 0),
                box=box.ASCII,
            )
        else:
            label = f"[!]  {severity.value.upper()} LEAK SIGNAL"
            banner = Panel(
                Align.center(Text(label, style=f"bold {style}")),
                border_style=style,
                padding=(1, 0),
                box=box.ASCII,
            )

        event_table = Table.grid(expand=True)
        event_table.add_column(style="dim", width=8)
        event_table.add_column()
        for event in self._events:
            event_table.add_row(
                time.strftime("%H:%M:%S"),
                Text(event, style="white"),
            )
        if not self._events:
            event_table.add_row("-", Text("Waiting for telemetry events...", style="dim"))

        alert_table = self._render_alerts(snapshot)
        return Panel(
            Group(
                banner,
                Text("ACTIVE DIAGNOSTICS", style="bold white"),
                alert_table,
                Rule(characters="-", style="magenta"),
                Text("EVENT LOG", style="bold white"),
                event_table,
            ),
            title="[bold magenta] DIAGNOSTICS [/bold magenta]",
            border_style="magenta",
            padding=(0, 1),
            box=box.ASCII,
        )

    def _render_alerts(self, snapshot: MetricsSnapshot | None) -> Table:
        table = Table.grid(expand=True, padding=(0, 1))
        table.add_column(width=9)
        table.add_column(width=8)
        table.add_column()
        if not snapshot or not snapshot.alerts:
            table.add_row(
                Text("CLEAR", style="green"),
                "-",
                Text("No monotonic growth detected", style="dim"),
            )
            return table

        for alert in snapshot.alerts:
            table.add_row(
                Text(alert.resource.value.upper(), style="bold white"),
                Text(
                    alert.severity.value.upper(),
                    style=f"bold {self._severity_style(alert.severity)}",
                ),
                Text(alert.message, style="white"),
            )
        return table

    def _render_footer(self) -> Panel:
        controls = Text()
        controls.append("[SPACE]", style="bold cyan")
        controls.append(" Pause/Resume   ", style="white")
        controls.append("[K]", style="bold red")
        controls.append(" Kill Process   ", style="white")
        controls.append("[E]", style="bold yellow")
        controls.append(" Export Log   ", style="white")
        controls.append("[Q]", style="bold magenta")
        controls.append(" Quit", style="white")
        return Panel(
            Align.center(controls),
            border_style="magenta",
            padding=(0, 1),
            box=box.ASCII,
        )

    def _sparkline(self, values: deque[float], color: str) -> RenderableType:
        if len(values) < 2:
            return Text("... waiting for trend samples ...", style="dim")
        points = list(values)
        low, high = min(points), max(points)
        if high == low:
            indexes = [3] * len(points)
        else:
            indexes = [
                round((point - low) / (high - low) * 7)
                for point in points
            ]
        glyphs = ".:-=+*#@"
        return Text("".join(glyphs[index] for index in indexes), style=color)

    @staticmethod
    def _progress_bar(value: float, width: int = 4) -> Text:
        bounded = min(max(value, 0.0), 100.0)
        complete = round((bounded / 100.0) * width)
        progress = Text("[")
        progress.append("#" * complete, style="cyan")
        progress.append("." * (width - complete), style="bright_black")
        progress.append(f"] {bounded:5.1f}%", style="bold cyan")
        return progress

    def _totals(self) -> dict[str, float]:
        if self.snapshot is None:
            return {
                "cpu_percent": 0.0,
                "rss_bytes": 0.0,
                "handles": 0.0,
                "threads": 0.0,
            }
        return self.snapshot.totals

    def _process_name(self, snapshot: MetricsSnapshot | None) -> str:
        if snapshot:
            for process in snapshot.processes:
                if process.pid == snapshot.root_pid:
                    return process.name
            if snapshot.processes:
                return snapshot.processes[0].name
        return Path(self.target_path).name

    def _status(self, snapshot: MetricsSnapshot | None) -> tuple[str, str]:
        if snapshot is None:
            return "STARTING", "yellow"
        if not snapshot.process_alive:
            return "EXITED", "red"
        if self.detector.is_paused or self.manager.is_paused:
            return "PAUSED", "yellow"
        return "RUNNING", "green"

    @staticmethod
    def _severity_style(severity: LeakSeverity) -> str:
        return {
            LeakSeverity.NONE: "green",
            LeakSeverity.LOW: "yellow",
            LeakSeverity.MEDIUM: "yellow",
            LeakSeverity.HIGH: "red",
            LeakSeverity.CRITICAL: "bright_red",
        }[severity]

    @staticmethod
    def _format_bytes(value: float) -> str:
        units = ("B", "KB", "MB", "GB", "TB")
        amount = max(0.0, value)
        for unit in units:
            if amount < 1024 or unit == units[-1]:
                return f"{amount:,.1f} {unit}"
            amount /= 1024
        return f"{value:,.1f} B"

    @staticmethod
    def _trend(values: deque[float]) -> str:
        if len(values) < 2:
            return "-"
        delta = values[-1] - values[-2]
        if abs(delta) < max(abs(values[-2]) * 0.01, 1):
            return "-"
        return "^" if delta > 0 else "v"

    def _append_event(self, message: str) -> None:
        self._events.append(message)

    @staticmethod
    def _snapshot_to_dict(snapshot: MetricsSnapshot) -> dict[str, Any]:
        data = asdict(snapshot)
        data["alerts"] = [
            {
                "resource": alert.resource.value,
                "severity": alert.severity.value,
                "message": alert.message,
                "slope_per_second": alert.slope_per_second,
                "window_seconds": alert.window_seconds,
                "current_value": alert.current_value,
                "baseline_value": alert.baseline_value,
            }
            for alert in snapshot.alerts
        ]
        data["severity"] = snapshot.severity.value
        return data

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
