"""Non-interactive monitoring for CI pipelines and scripted runs."""

from __future__ import annotations

import os
import time
from pathlib import Path

from rich.console import Console
from rich.text import Text

from .dashboard import DashboardApp
from .events import LeakResource, LeakSeverity, severity_rank
from .report import DEFAULT_REPORT_DIR
from .session import MonitorSession
from .theme import PALETTE
from .units import format_duration

EXIT_PASS = 0
EXIT_LEAK = 1
EXIT_ERROR = 2
EXIT_INTERRUPTED = 130

FAIL_ON_CHOICES = ("low", "medium", "high", "critical")

_LEVEL_TAGS = {
    "ok": ("OK", PALETTE.ok),
    "info": ("INFO", PALETTE.accent),
    "warn": ("WARN", PALETTE.warn),
    "alert": ("ALERT", PALETTE.alert),
    "error": ("ERROR", PALETTE.danger),
    "stat": ("STAT", PALETTE.muted),
}


class HeadlessRunner(MonitorSession):
    """Monitor a target without the dashboard and turn the result into an exit code.

    Events and periodic statistics are printed as plain lines suited to CI
    logs. Monitoring ends at the time limit, when the monitored tree exits,
    or on Ctrl+C; a report is then exported and ``run()`` returns
    ``EXIT_LEAK`` if any leak signal reached ``fail_on``.
    """

    VERSION = DashboardApp.VERSION
    POLL_SECONDS = 0.1
    PROGRESS_SECONDS = 5.0

    def __init__(
        self,
        target_path: str | None,
        args: list[str] | None = None,
        console: Console | None = None,
        report_dir: str | Path = DEFAULT_REPORT_DIR,
        attach_pid: int | None = None,
        timeout: float | None = None,
        fail_on: LeakSeverity = LeakSeverity.HIGH,
    ) -> None:
        if fail_on == LeakSeverity.NONE:
            raise ValueError("fail_on must be a leak severity, not NONE")
        super().__init__(
            target_path,
            args=args,
            console=console,
            report_dir=report_dir,
            attach_pid=attach_pid,
            timeout=timeout,
        )
        self.fail_on = fail_on
        self._github_actions = os.environ.get("GITHUB_ACTIONS") == "true"
        self._export_error: str | None = None
        self._interrupted = False

    @property
    def breached(self) -> bool:
        """Whether any leak signal reached the failure threshold."""
        return severity_rank(self.worst_severity) >= severity_rank(self.fail_on)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> int:
        """Monitor until the limit, tree exit, or Ctrl+C; return an exit code."""
        self._print_banner()
        try:
            self.start()
        except Exception as exc:
            self._print_line("error", f"Could not start monitoring: {exc}")
            return EXIT_ERROR

        try:
            self._monitor()
        except KeyboardInterrupt:
            self._interrupted = True
            self._append_event("Interrupted; stopping early", "warn")
        finally:
            self.shutdown()
        self.poll()

        self._export()
        self._print_summary()
        return self.exit_status()

    def _monitor(self) -> None:
        """Poll until the monitored tree exits or the time limit passes."""
        next_progress = time.monotonic() + self.PROGRESS_SECONDS
        while True:
            self.poll()
            if self.tree_finished:
                self._record_final_sample()
                self._append_event("Monitored process tree exited", "info")
                return
            if self.timed_out:
                self._append_event(
                    f"Time limit of {format_duration(self.timeout or 0.0)} reached", "info"
                )
                return
            now = time.monotonic()
            if now >= next_progress:
                self._print_progress()
                next_progress = now + self.PROGRESS_SECONDS
            time.sleep(self.POLL_SECONDS)

    def _record_final_sample(self) -> None:
        """Sample once more so the exit itself reaches the log and report.

        The detector samples on its own schedule and may not have seen the
        tree disappear yet. Stopping it first keeps the extra sample from
        racing the background thread.
        """
        self.detector.stop()
        self.poll()
        if self.detector.is_running:
            return
        try:
            self._accept_snapshot(self.detector.sample_once())
        except Exception as exc:  # the tree is gone; a failed sample is harmless
            self._print_line("warn", f"Final sample failed: {exc}")

    def exit_status(self) -> int:
        """Map the session outcome to the process exit code."""
        if self.breached:
            return EXIT_LEAK
        if self._interrupted:
            return EXIT_INTERRUPTED
        if self._export_error is not None:
            return EXIT_ERROR
        return EXIT_PASS

    def _export(self) -> None:
        try:
            self.export_log()
        except OSError as exc:
            self._export_error = str(exc)
            self._print_line("error", f"Report export failed: {exc}")

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def _append_event(self, message: str, level: str = "info") -> None:
        super()._append_event(message, level)
        self._print_line(level, message)

    def _print_line(self, level: str, message: str | Text) -> None:
        tag, color = _LEVEL_TAGS.get(level, _LEVEL_TAGS["info"])
        line = Text.assemble(
            (self._format_duration(self._uptime()), PALETTE.muted),
            "  ",
            (f"{tag:<5}", f"bold {color}"),
            "  ",
            message,
        )
        self.console.print(line, soft_wrap=True)

    def _print_banner(self) -> None:
        sep = self.glyphs.sep
        if self.attached:
            action = f"attaching to PID {self.attach_pid} ({Path(self.target_path).name})"
        else:
            action = f"launching {Path(self.target_path).name}"
        limit = (
            f"time limit {format_duration(self.timeout)}"
            if self.timeout
            else "until the process tree exits"
        )
        self.console.print(
            Text.assemble(
                (f"{self.TITLE} {self.VERSION} headless", f"bold {PALETTE.accent}"),
                (f" {sep} {action} {sep} {limit} {sep} ", PALETTE.muted),
                (f"fails on {self.fail_on.value.upper()} or above", PALETTE.muted),
            ),
            soft_wrap=True,
        )

    def _print_progress(self) -> None:
        totals = self._totals()
        sep = self.glyphs.sep
        processes = len(self.snapshot.processes) if self.snapshot else 0
        severity = self.snapshot.severity if self.snapshot else LeakSeverity.NONE
        active = ", ".join(
            alert.resource.value for alert in (self.snapshot.alerts if self.snapshot else ())
        )
        signal = severity.value.upper() + (f" ({active})" if active else "")
        self._print_line(
            "stat",
            f"cpu {totals.get('cpu_percent', 0.0):.1f}% {sep} "
            f"rss {self._format_bytes(totals.get('rss_bytes', 0.0))} {sep} "
            f"private {self._format_bytes(totals.get('private_bytes', 0.0))} {sep} "
            f"handles {int(totals.get('handles', 0.0)):,} {sep} "
            f"threads {int(totals.get('threads', 0.0)):,} {sep} "
            f"{processes} process{'es' if processes != 1 else ''} {sep} "
            f"signal {signal}",
        )

    def _print_summary(self) -> None:
        sep = self.glyphs.sep
        status = self.exit_status()
        verdict, color = {
            EXIT_PASS: ("PASS", PALETTE.ok),
            EXIT_LEAK: ("FAIL", PALETTE.danger),
            EXIT_ERROR: ("ERROR", PALETTE.danger),
            EXIT_INTERRUPTED: ("INTERRUPTED", PALETTE.warn),
        }[status]
        worst = self.worst_severity
        reason = (
            "no leak signals observed"
            if worst == LeakSeverity.NONE
            else f"worst leak signal {worst.value.upper()}"
        )

        if self.attached:
            mode = "attached, " + ("left running" if self._left_running else "no longer running")
        elif self.stopped_target:
            mode = "launched, stopped by DrainTracer"
        elif self.manager.exit_code is not None:
            mode = f"launched, exited with code {self.manager.exit_code}"
        else:
            mode = "launched"
        signals = f" {sep} ".join(
            f"{resource.value} {self.worst_severity_for(resource).value.upper()}"
            for resource in LeakResource
        )
        rows = [
            ("target", f"{Path(self.target_path).name} {sep} PID {self.root_pid or '-'} ({mode})"),
            ("duration", f"{self._format_duration(self._uptime())} {sep} "
                         f"{self._sample_count:,} samples"),
            ("peaks", f"RSS {self._format_bytes(self._peaks['rss_bytes'])} {sep} "
                      f"private {self._format_bytes(self._peaks['private_bytes'])} {sep} "
                      f"{int(self._peaks['handles']):,} handles {sep} "
                      f"{int(self._peaks['threads']):,} threads"),
            ("signals", signals),
        ]
        if self._last_report is not None:
            rows.append(("report", str(self._last_report)))

        self.console.print()
        for label, value in rows:
            self.console.print(
                Text.assemble((f"  {label:<10}", f"bold {PALETTE.muted}"), value),
                soft_wrap=True,
            )
        self.console.print(
            Text.assemble(
                ("RESULT ", "bold"),
                (f" {verdict} ", f"bold {PALETTE.ink} on {color}"),
                f"  {reason} {sep} fails on {self.fail_on.value.upper()} or above",
            ),
            soft_wrap=True,
        )
        self._annotate(status, reason)

    def _annotate(self, status: int, reason: str) -> None:
        """Surface the verdict as a GitHub Actions annotation on CI runs."""
        if not self._github_actions:
            return
        if status == EXIT_LEAK:
            command = "error"
        elif self.worst_severity != LeakSeverity.NONE or status != EXIT_PASS:
            command = "warning"
        else:
            return
        message = (
            f"{Path(self.target_path).name}: {reason} "
            f"(fails on {self.fail_on.value.upper()} or above)"
        )
        if self._last_report is not None:
            message += f". Report: {self._last_report}"
        title = _escape_property("DrainTracer leak check")
        self.console.print(
            f"::{command} title={title}::{_escape_data(message)}",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )


def _escape_data(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(value: str) -> str:
    return _escape_data(value).replace(":", "%3A").replace(",", "%2C")


__all__ = [
    "EXIT_ERROR",
    "EXIT_INTERRUPTED",
    "EXIT_LEAK",
    "EXIT_PASS",
    "FAIL_ON_CHOICES",
    "HeadlessRunner",
]
