"""Monitoring session state shared by the dashboard and other front ends."""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path
from typing import Any

from rich.console import Console

from .events import (
    LeakAlert,
    LeakResource,
    LeakSeverity,
    MetricsSnapshot,
    severity_rank,
)
from .handle_types import describe_type_growth
from .html_report import write_html_report
from .leak_detector import LeakDetector
from .process_runner import ProcessManager, describe_pid
from .report import (
    DEFAULT_REPORT_DIR,
    REPORT_SCHEMA,
    iso_timestamp,
    new_report_id,
    snapshot_to_dict,
    write_json_report,
)
from .theme import select_glyphs
from .units import format_bytes, format_clock

HISTORY_KEYS = ("cpu_percent", "rss_bytes", "private_bytes", "handles", "threads")


class MonitorSession:
    """Launch or attach to a target, collect its snapshots, and export them.

    The session owns the process manager, the background detector, and the
    telemetry gathered so far: chart history, peaks, the event log, and the
    worst severity seen. Front ends subclass it and add presentation only;
    they must define the ``VERSION`` class attribute written to reports.
    """

    TITLE = "DRAINTRACER"
    VERSION: str
    HISTORY_LIMIT = 240
    EVENT_LIMIT = 200
    SNAPSHOT_LIMIT = 7200

    def __init__(
        self,
        target_path: str | None,
        args: list[str] | None = None,
        console: Console | None = None,
        report_dir: str | Path = DEFAULT_REPORT_DIR,
        attach_pid: int | None = None,
        timeout: float | None = None,
    ) -> None:
        if timeout is not None and not timeout > 0:
            raise ValueError("timeout must be a positive number of seconds")
        self.timeout = timeout
        self._deadline: float | None = None
        self.attach_pid = attach_pid
        if attach_pid is not None:
            self.target_path = target_path or describe_pid(attach_pid)
            self.manager = ProcessManager(None)
        else:
            if not target_path:
                raise ValueError("target_path is required unless attach_pid is given")
            self.target_path = str(Path(target_path).resolve())
            self.manager = ProcessManager(self.target_path, args=args)
        self.console = console or Console()
        self.detector = LeakDetector(self.manager)
        self.report_dir = Path(report_dir)

        self.glyphs = select_glyphs(self.console)
        self.snapshot: MetricsSnapshot | None = None
        self._history: dict[str, deque[tuple[float, float]]] = {
            key: deque(maxlen=self.HISTORY_LIMIT) for key in HISTORY_KEYS
        }
        self._peaks: dict[str, float] = {key: 0.0 for key in HISTORY_KEYS}
        self._snapshot_history: deque[dict[str, Any]] = deque(maxlen=self.SNAPSHOT_LIMIT)
        self._events: deque[tuple[float, str, str]] = deque(maxlen=self.EVENT_LIMIT)
        self._resource_severity: dict[LeakResource, LeakSeverity] = {}
        self._worst_by_resource: dict[LeakResource, LeakSeverity] = {}
        self._handle_types: dict[str, int] | None = None
        self._worst_severity = LeakSeverity.NONE
        self._sample_count = 0
        self._started_at: float | None = None
        self._ended_at: float | None = None
        self._root_pid: int | None = None
        self._stopped_target = False
        self._last_report: Path | None = None
        self._last_report_id: str | None = None
        self._last_html_report: Path | None = None
        self._left_running = False

    @property
    def attached(self) -> bool:
        return self.attach_pid is not None

    @property
    def root_pid(self) -> int | None:
        """Root PID of the session; kept after the manager releases it."""
        return self.manager.root_pid or self._root_pid or self.attach_pid

    @property
    def stopped_target(self) -> bool:
        """Whether shutdown had to terminate a launched tree that was still running."""
        return self._stopped_target

    @property
    def time_remaining(self) -> float | None:
        """Seconds left before the time limit, or ``None`` without one."""
        if self._deadline is None:
            return None
        return max(0.0, self._deadline - time.monotonic())

    @property
    def timed_out(self) -> bool:
        remaining = self.time_remaining
        return remaining is not None and remaining <= 0.0

    @property
    def worst_severity(self) -> LeakSeverity:
        """Strongest leak signal observed during the session."""
        return self._worst_severity

    def worst_severity_for(self, resource: LeakResource) -> LeakSeverity:
        """Strongest signal observed for one resource during the session."""
        return self._worst_by_resource.get(resource, LeakSeverity.NONE)

    @property
    def tree_finished(self) -> bool:
        """Whether the root and every tracked descendant have exited."""
        return (
            self._started_at is not None
            and not self.manager.is_running
            and not self.manager.has_tracked_processes
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Launch or attach, take a first sample, and start background sampling.

        If anything fails, including Ctrl+C, a launched target is killed and
        an attached one released before the error propagates.
        """
        try:
            if self.attached:
                self.manager.attach(self.attach_pid)
            else:
                self.manager.start()
            self._root_pid = self.manager.root_pid
            self._started_at = time.time()
            if self.timeout is not None:
                self._deadline = time.monotonic() + self.timeout
            self._accept_snapshot(self.detector.sample_once())
            self.detector.start()
        except BaseException:
            self._abort_start()
            raise
        verb = "Attached to running" if self.attached else "Launched"
        self._append_event(
            f"{verb} {Path(self.target_path).name} "
            f"{self.glyphs.sep} PID {self.manager.root_pid}",
            "ok",
        )
        self._warn_if_inaccessible()

    def _abort_start(self) -> None:
        self.detector.stop()
        if self.attached:
            self.manager.detach()
        elif self.manager.has_tracked_processes:
            self.manager.kill()

    def shutdown(self) -> None:
        """Stop sampling; terminate launched targets, release attached ones."""
        if self._started_at is not None and self._ended_at is None:
            self._ended_at = time.time()
        self.detector.stop()
        if self.attached:
            self._left_running = self.manager.is_running
            self.manager.detach()
        elif self.manager.is_running or self.manager.has_tracked_processes:
            self._stopped_target = True
            self.manager.terminate()

    def poll(self) -> None:
        """Apply every snapshot the detector published since the last poll."""
        for snapshot in self.detector.stream.drain():
            self._accept_snapshot(snapshot)

    def _warn_if_inaccessible(self) -> None:
        snapshot = self.snapshot
        if snapshot is None:
            return
        root = next((p for p in snapshot.processes if p.pid == snapshot.root_pid), None)
        if root is not None and root.access_denied:
            self._append_event(
                "Access denied: metrics unavailable (try an elevated terminal)", "error"
            )

    # ------------------------------------------------------------------
    # Snapshot intake and the event log
    # ------------------------------------------------------------------

    def _accept_snapshot(self, snapshot: MetricsSnapshot) -> None:
        previous = self.snapshot
        self.snapshot = snapshot

        if not snapshot.monitoring_paused:
            self._sample_count += 1
            for key in HISTORY_KEYS:
                value = float(snapshot.totals.get(key, 0.0))
                self._history[key].append((snapshot.timestamp, value))
                self._peaks[key] = max(self._peaks[key], value)
            if snapshot.handle_types is not None:
                self._handle_types = dict(snapshot.handle_types)
        self._snapshot_history.append(snapshot_to_dict(snapshot))

        if severity_rank(snapshot.severity) > severity_rank(self._worst_severity):
            self._worst_severity = snapshot.severity
        for alert in snapshot.alerts:
            worst = self._worst_by_resource.get(alert.resource, LeakSeverity.NONE)
            if severity_rank(alert.severity) > severity_rank(worst):
                self._worst_by_resource[alert.resource] = alert.severity

        if previous and snapshot.process_alive is False and previous.process_alive:
            code = self.manager.exit_code
            suffix = f" with code {code}" if code is not None else ""
            self._append_event(f"Target process exited{suffix}", "warn")

        sampling_error = snapshot.metadata.get("sampling_error")
        if sampling_error and (
            previous is None or previous.metadata.get("sampling_error") != sampling_error
        ):
            self._append_event(f"Sampling error: {sampling_error}", "error")

        if not snapshot.monitoring_paused:
            self._log_signal_transitions(snapshot)

    def _log_signal_transitions(self, snapshot: MetricsSnapshot) -> None:
        """Log when a resource signal appears, changes tier, or clears.

        Alert messages carry live numbers that change every sample, so the
        log tracks per-resource severity instead of message text.
        """
        alerts = {alert.resource: alert for alert in snapshot.alerts}
        for resource in LeakResource:
            alert = alerts.get(resource)
            current = alert.severity if alert else LeakSeverity.NONE
            before = self._resource_severity.get(resource, LeakSeverity.NONE)
            if current == before:
                continue
            self._resource_severity[resource] = current
            name = resource.value.capitalize()
            if alert is None:
                self._append_event(f"{name} signal cleared", "ok")
            elif before == LeakSeverity.NONE:
                self._append_event(
                    f"{name} leak signal {current.value.upper()} "
                    f"{self.glyphs.sep} {self._alert_summary(alert)}",
                    self._severity_level(current),
                )
            else:
                verb = "escalated" if severity_rank(current) > severity_rank(before) else "eased"
                self._append_event(
                    f"{name} signal {verb} to {current.value.upper()}",
                    self._severity_level(current),
                )

    def _append_event(self, message: str, level: str = "info") -> None:
        self._events.append((time.time(), message, level))

    def _alert_summary(self, alert: LeakAlert) -> str:
        delta = alert.current_value - alert.baseline_value
        if alert.resource == LeakResource.MEMORY:
            amount = self._format_bytes(delta)
            rate = f"{self._format_bytes(alert.slope_per_second)}/s"
        else:
            amount = f"{delta:,.0f}"
            rate = f"{alert.slope_per_second:,.1f}/s"
        summary = f"+{amount} in {alert.window_seconds:.0f}s {self.glyphs.sep} {rate}"
        types = describe_type_growth(alert.growth_by_type or {})
        return f"{summary} {self.glyphs.sep} {types}" if types else summary

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------

    def export_log(self) -> Path:
        """Write ``leak_report_<uuid>.json`` and its HTML companion.

        Every export gets a fresh UUID so repeated exports never overwrite
        an earlier report. Returns the JSON path. The HTML report is best
        effort: if it cannot be rendered or written, the JSON report still
        stands and the failure is logged.
        """
        report = self.build_report()
        path = write_json_report(report, self.report_dir)
        self._last_report = path
        self._last_report_id = report["report_id"]
        self._last_html_report = None
        try:
            self._last_html_report = write_html_report(report, self.report_dir)
        except Exception as exc:  # never lose the JSON report to a rendering bug
            self._append_event(f"HTML report failed: {exc}", "error")
        return path

    def build_report(self) -> dict[str, Any]:
        """Assemble the report dictionary for the session so far."""
        return {
            "report_id": new_report_id(),
            "report_schema": REPORT_SCHEMA,
            "created_at": iso_timestamp(),
            "session_started_at": iso_timestamp(self._started_at) if self._started_at else None,
            "session_ended_at": iso_timestamp(self._ended_at) if self._ended_at else None,
            "app": self.TITLE,
            "version": self.VERSION,
            "target_path": self.target_path,
            "root_pid": self.root_pid,
            "mode": "attach" if self.attached else "launch",
            "target_exit_code": None if self._stopped_target else self.manager.exit_code,
            "events": [
                f"{time.strftime('%H:%M:%S', time.localtime(stamp))} {message}"
                for stamp, message, _level in self._events
            ],
            "event_log": [
                {
                    "time": iso_timestamp(stamp),
                    "timestamp": stamp,
                    "level": level,
                    "message": message,
                }
                for stamp, message, level in self._events
            ],
            "snapshots": list(self._snapshot_history),
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _totals(self) -> dict[str, float]:
        if self.snapshot is None:
            return {key: 0.0 for key in HISTORY_KEYS}
        return self.snapshot.totals

    def _uptime(self) -> float:
        """Seconds monitored so far; frozen once the session shuts down."""
        if self._started_at is None:
            return 0.0
        end = self._ended_at if self._ended_at is not None else time.time()
        return max(0.0, end - self._started_at)

    def _process_name(self, snapshot: MetricsSnapshot | None) -> str:
        if snapshot:
            for process in snapshot.processes:
                if process.pid == snapshot.root_pid:
                    return process.name
            if snapshot.processes:
                return snapshot.processes[0].name
        return Path(self.target_path).name

    @staticmethod
    def _severity_level(severity: LeakSeverity) -> str:
        if severity in {LeakSeverity.HIGH, LeakSeverity.CRITICAL}:
            return "error"
        if severity == LeakSeverity.NONE:
            return "ok"
        return "alert"

    _format_bytes = staticmethod(format_bytes)
    _format_duration = staticmethod(format_clock)


__all__ = ["HISTORY_KEYS", "MonitorSession"]
