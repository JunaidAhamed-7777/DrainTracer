"""Monitoring session state shared by the dashboard and other front ends."""

from __future__ import annotations

import errno
import sys
import time
from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import Any

from rich.console import Console

from .config import MonitorSettings
from .events import (
    RESOURCE_METRICS,
    LeakAlert,
    LifecycleEvent,
    LifecycleKind,
    LeakResource,
    LeakSeverity,
    MetricsSnapshot,
    MetricsStream,
    MonitorDiagnostics,
    ProcessCounts,
    ProcessSummary,
    TimelineKind,
    TimelineMark,
    severity_rank,
)
from .experiment import experiment_record
from .handle_types import describe_type_growth
from .history import SnapshotHistory
from .html_report import write_html_report
from .leak_detector import LeakDetector
from .process_runner import (
    ProcessAccessDeniedError,
    ProcessManager,
    ProcessNotFoundError,
    describe_pid,
)
from .report import (
    DEFAULT_REPORT_DIR,
    REPORT_SCHEMA,
    iso_timestamp,
    new_report_id,
    coverage_to_dict,
    process_summary_to_dict,
    report_directory,
    snapshot_to_dict,
    write_json_report,
)
from .session_facts import SessionFacts
from .theme import select_glyphs
from .units import format_bytes, format_clock, format_duration

HISTORY_KEYS = ("cpu_percent", "rss_bytes", "private_bytes", "handles", "threads")
SIGNAL_METRICS = tuple(RESOURCE_METRICS.values())
# Bounds for session-long records that grow with the target's behaviour.
UNREADABLE_PID_LIMIT = 50
MONITOR_ERROR_LIMIT = 20
MEASUREMENT_CONCLUSIVE = "conclusive"
MEASUREMENT_PARTIAL = "partial"
MEASUREMENT_INCONCLUSIVE = "inconclusive"
# Lifecycle events of one kind in one sample beyond this are logged as one line.
LIFECYCLE_DETAIL_LIMIT = 3
# Windows errors for files that cannot be started as programs: bad image,
# bad format, wrong signature, or the wrong machine type.
_NOT_A_PROGRAM = {129, 191, 193, 216}


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
    # Chart annotations, lifecycle events kept for the process inspector,
    # and user markers exported with reports.
    TIMELINE_LIMIT = 500
    LIFECYCLE_LIMIT = 256
    MARKER_LIMIT = 500

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
        if timeout is not None and not timeout > 0:
            raise ValueError("timeout must be a positive number of seconds")
        # Detector configuration and its origin (profile, config file).
        # report_dir and timeout stay explicit so callers decide on them.
        self.settings = settings or MonitorSettings()
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
        self.detector = LeakDetector(
            self.manager,
            # Sampling continues while the main thread is stalled, for
            # example on a console paused by a text selection. Keep as many
            # snapshots as the session would, so none are dropped meanwhile.
            stream=MetricsStream(max_queue_size=self.SNAPSHOT_LIMIT),
            sample_interval_ms=self.settings.sample_interval_ms,
            window_seconds=self.settings.windows,
            thresholds=self.settings.thresholds,
            warmup_seconds=self.settings.warmup,
        )
        self.report_dir = Path(report_dir)

        self.glyphs = select_glyphs(self.console)
        self.snapshot: MetricsSnapshot | None = None
        self._history: dict[str, deque[tuple[float, float]]] = {
            key: deque(maxlen=self.HISTORY_LIMIT) for key in HISTORY_KEYS
        }
        self._peaks: dict[str, float] = {key: 0.0 for key in HISTORY_KEYS}
        # Frozen snapshots are about a third the size of their dict form, so
        # conversion waits until a report is exported. A long session is
        # thinned rather than cut, so reports always cover all of it.
        self._snapshot_history = SnapshotHistory(self.SNAPSHOT_LIMIT)
        # Exact peaks, episodes and counts from every snapshot, which the
        # thinned history above cannot promise.
        self._facts = SessionFacts()
        # Diagnostics from the newest snapshot of any kind: a failed sample
        # keeps the last good snapshot on screen but still reports its count.
        self._diagnostics: MonitorDiagnostics | None = None
        self._events: deque[tuple[float, str, str]] = deque(maxlen=self.EVENT_LIMIT)
        self._resource_severity: dict[LeakResource, LeakSeverity] = {}
        self._worst_by_resource: dict[LeakResource, LeakSeverity] = {}
        self._last_alert_by_resource: dict[LeakResource, LeakAlert] = {}
        self._handle_types: dict[str, int] | None = None
        self._last_sampling_error: str | None = None
        self._worst_severity = LeakSeverity.NONE
        self._sample_count = 0
        self._started_at: float | None = None
        self._ended_at: float | None = None
        self._root_pid: int | None = None
        self._stopped_target = False
        self._stopped_leftovers = False
        self._last_report: Path | None = None
        self._last_report_id: str | None = None
        self._last_html_report: Path | None = None
        self._last_report_data: dict[str, Any] | None = None
        self._left_running = False
        # Measurement coverage of the signal metrics over the session: samples
        # with live processes, ones missing any (partial, which includes
        # unavailable), and ones with nothing readable (unavailable).
        self._coverage_samples = 0
        self._partial_samples = {metric: 0 for metric in SIGNAL_METRICS}
        self._unavailable_samples = {metric: 0 for metric in SIGNAL_METRICS}
        self._unreadable_pids: dict[int, str] = {}
        self._current_unreadable: set[int] = set()
        self._monitor_errors: list[str] = []
        self._timeline: deque[TimelineMark] = deque(maxlen=self.TIMELINE_LIMIT)
        self._lifecycle_history: deque[LifecycleEvent] = deque(maxlen=self.LIFECYCLE_LIMIT)
        self._markers: list[TimelineMark] = []
        self._marker_count = 0

    @property
    def attached(self) -> bool:
        return self.attach_pid is not None

    @property
    def root_pid(self) -> int | None:
        """Root PID of the session; kept after the manager releases it."""
        snapshot_pid = self.snapshot.root_pid if self.snapshot is not None else None
        return self.manager.root_pid or self._root_pid or self.attach_pid or snapshot_pid

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
    def conclusive(self) -> bool:
        """Whether a leak window was ever judged, so a clean result means something."""
        return self.detector.detection_started

    def inconclusive_reason(self) -> str:
        """Why no leak window was judged, for summaries of clean runs."""
        detector = self.detector
        if detector.warmup_seconds and detector.warming_up:
            return (
                f"the {format_duration(detector.warmup_seconds)} warm-up never finished, "
                "so leak detection did not run"
            )
        if self._coverage_samples and all(
            self._unavailable_samples[metric] == self._coverage_samples for metric in SIGNAL_METRICS
        ):
            return (
                "no process in the tree could be measured (access denied or unavailable), "
                "so no leak window was judged"
            )
        return (
            f"monitoring ended before the {format_duration(detector.window_seconds[0])} "
            "window filled, so no leak window was judged"
        )

    @property
    def partial_coverage(self) -> bool:
        """Whether a signal metric missed some live processes on any sample."""
        return any(self._partial_samples.values())

    @property
    def measurement_outcome(self) -> str:
        """``conclusive``, ``partial`` (judged, but some processes unreadable), or ``inconclusive``."""
        if not self.conclusive:
            return MEASUREMENT_INCONCLUSIVE
        return MEASUREMENT_PARTIAL if self.partial_coverage else MEASUREMENT_CONCLUSIVE

    def coverage_text(self) -> str | None:
        """How much of the tree went unmeasured, or ``None`` when coverage was full.

        For example ``1 process unreadable (PID 4242) in 40 of 120 samples``.
        """
        if not self.partial_coverage:
            return None
        affected = max(self._partial_samples.values())
        pids = sorted(self._unreadable_pids)
        count = len(pids)
        shown = ", ".join(str(pid) for pid in pids[:3]) + (", ..." if count > 3 else "")
        noun = "process" if count == 1 else "processes"
        label = f"PID {shown}" if count == 1 else f"PIDs {shown}"
        return (
            f"{count} {noun} unreadable ({label}) in {affected:,} of "
            f"{self._coverage_samples:,} samples"
        )

    @property
    def target_failed(self) -> bool:
        """Whether a launched target exited on its own with a non-zero code."""
        if self.attached or self._stopped_target:
            return False
        code = self.manager.exit_code
        return code is not None and code != 0

    @property
    def monitor_errors(self) -> tuple[str, ...]:
        """Distinct sampling and export problems seen this session, oldest first."""
        return tuple(self._monitor_errors)

    def _note_monitor_error(self, message: str) -> None:
        if message not in self._monitor_errors and len(self._monitor_errors) < MONITOR_ERROR_LIMIT:
            self._monitor_errors.append(message)

    def outcome_text(self) -> str:
        """How the session left the target: running, exited, or stopped."""
        if self.attached:
            return "attached, " + ("left running" if self._left_running else "no longer running")
        if self.stopped_target:
            return "launched, stopped by DrainTracer"
        code = self.manager.exit_code
        outcome = f"launched, exited with code {code}" if code is not None else "launched"
        if self._stopped_leftovers:
            outcome += "; DrainTracer stopped the processes it left running"
        return outcome

    def signals_text(self) -> str:
        """Strongest signal per resource, naming the handle types that grew."""
        parts = []
        for resource in LeakResource:
            worst = self.worst_severity_for(resource)
            part = f"{resource.value} {worst.value.upper() if worst != LeakSeverity.NONE else 'clear'}"
            alert = self._last_alert_by_resource.get(resource)
            growth = describe_type_growth(alert.growth_by_type or {}, limit=1) if alert else ""
            if worst != LeakSeverity.NONE and growth:
                part += f" ({growth.removeprefix('by type: ')})"
            parts.append(part)
        return f" {self.glyphs.sep} ".join(parts)

    def warmup_text(self) -> str | None:
        """Warm-up length and when it finished, or ``None`` without one."""
        detector = self.detector
        if not detector.warmup_seconds:
            return None
        finished = detector.warmup_completed_at
        if finished is not None and self._started_at is not None:
            done = f"finished at +{self._format_duration(finished - self._started_at)}"
        else:
            done = "did not finish"
        return f"{format_duration(detector.warmup_seconds)} {self.glyphs.sep} {done}"

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
            self._log_start()
        except BaseException:
            self._abort_start()
            raise

    def _log_start(self) -> None:
        verb = "Attached to running" if self.attached else "Launched"
        self._append_event(
            f"{verb} {Path(self.target_path).name} "
            f"{self.glyphs.sep} PID {self.manager.root_pid}",
            "ok",
        )
        if sys.platform.startswith("linux"):
            self._append_event(
                "Linux counters: private bytes measure USS (private resident memory); "
                "handles count open file descriptors",
                "info",
            )
        if self.manager.contains_monitor:
            self._append_event(
                "DrainTracer runs inside this process tree, so its own branch is "
                "left out and pause and kill are disabled",
                "warn",
            )
        self._warn_if_inaccessible()
        if self.detector.warmup_seconds:
            self._append_event(
                f"Warming up for {format_duration(self.detector.warmup_seconds)}; "
                "leak detection starts after that",
                "info",
            )

    def startup_hint(self, error: BaseException) -> str | None:
        """A suggestion for a startup failure, or ``None`` when there is none."""
        cause = error.__cause__
        if isinstance(error, ProcessNotFoundError):
            if self.attached:
                if sys.platform == "win32":
                    return "Check the PID in Task Manager or with tasklist; the process may have exited."
                return "Check the PID with ps or top; the process may have exited."
            return "Check the path, or run without --target to pick the file in a dialog."
        if isinstance(error, (ProcessAccessDeniedError, PermissionError)) or isinstance(
            cause, PermissionError
        ):
            if sys.platform == "win32":
                return "Run the terminal as administrator to start or inspect this process."
            return (
                "Check the target's execute permission and ownership. Monitoring another "
                "user's process may require elevated privileges or different /proc permissions."
            )
        if isinstance(cause, OSError) and getattr(cause, "winerror", None) in _NOT_A_PROGRAM:
            return (
                "That file is not a Windows program. DrainTracer runs .exe files directly, "
                ".py files with Python, and .bat or .cmd files with cmd.exe."
            )
        if isinstance(cause, OSError) and cause.errno == errno.ENOEXEC:
            return (
                "Use a Linux executable or a script with an executable permission and "
                "a valid #! interpreter line. Python .py files use DrainTracer's Python."
            )
        return None

    def _abort_start(self) -> None:
        self.detector.stop()
        if self.attached:
            self.manager.detach()
        elif self.manager.has_tracked_processes:
            self.manager.kill()

    def shutdown(self) -> None:
        """Stop sampling; terminate launched targets, release attached ones.

        Graceful termination can take a few seconds. If Ctrl+C interrupts
        it, or a step fails, cleanup finishes at once (kill, or resume and
        detach) before the error propagates, so the target is never
        abandoned half-handled.
        """
        if self._started_at is not None and self._ended_at is None:
            self._ended_at = time.time()
        try:
            self.detector.stop()
            if self.attached:
                self._left_running = self.manager.is_running
                self.manager.detach()
            elif self.manager.is_running or self.manager.has_tracked_processes:
                self._mark_stopping()
                self.manager.terminate()
        except BaseException:
            self._force_cleanup()
            raise

    def _mark_stopping(self) -> None:
        """Before ending a tree, note whether that stops the root or only its leftovers.

        A root that already exited keeps its own exit code in the summary
        and report even when processes it started are still running.
        """
        if self.manager.is_running:
            self._stopped_target = True
        elif self.manager.has_tracked_processes and not self._stopped_leftovers:
            self._stopped_leftovers = True
            count = len(self.manager.tracked_pids)
            self._append_event(
                f"Stopping {count} process{'es' if count != 1 else ''} the target left running",
                "warn",
            )

    def _force_cleanup(self) -> None:
        """Finish cleanup without waiting; used when shutdown is interrupted or fails."""
        try:
            try:
                self.detector.stop()
            finally:
                # Release or kill the target even if the sampler will not stop.
                if self.attached:
                    self._left_running = self._left_running or self.manager.is_running
                    self.manager.detach()
                elif self.manager.is_running or self.manager.has_tracked_processes:
                    self._mark_stopping()
                    self.manager.kill()
        except KeyboardInterrupt:
            pass  # A third Ctrl+C: stop trying rather than loop.

    def poll(self) -> int:
        """Apply every snapshot published since the last poll; return how many."""
        snapshots = self.detector.stream.drain()
        for snapshot in snapshots:
            self._accept_snapshot(snapshot)
        return len(snapshots)

    def _warn_if_inaccessible(self) -> None:
        snapshot = self.snapshot
        if snapshot is None:
            return
        root = next((p for p in snapshot.processes if p.pid == snapshot.root_pid), None)
        if root is not None and root.access_denied:
            self._append_event(
                "Access denied: metrics unavailable (try an elevated terminal)", "error"
            )
            return
        unreadable = snapshot.unmeasured_pids
        if unreadable:
            count = len(unreadable)
            self._append_event(
                f"{count} process{'es' if count != 1 else ''} in the tree cannot be read "
                f"({', '.join(str(pid) for pid in unreadable[:3])}{', ...' if count > 3 else ''}); "
                "totals leave them out",
                "warn",
            )

    # ------------------------------------------------------------------
    # Snapshot intake and the event log
    # ------------------------------------------------------------------

    def _accept_snapshot(self, snapshot: MetricsSnapshot) -> None:
        self._facts.observe(snapshot)
        if snapshot.diagnostics is not None:
            self._diagnostics = snapshot.diagnostics
        # Per-process summaries describe the latest state only; keeping them
        # on every stored snapshot would multiply memory by the tree size.
        self._snapshot_history.append(
            replace(snapshot, process_summaries=(), exited_processes=())
            if snapshot.process_summaries or snapshot.exited_processes
            else snapshot
        )
        failure = snapshot.metadata.get("sampling_error")
        if failure:
            # A failed sample carries no measurements: keep showing the last
            # good one, and do not let its empty alert list clear signals.
            if failure != self._last_sampling_error:
                self._append_event(f"Sampling error: {failure}", "error")
                self._note_monitor_error(f"sampling error: {failure}")
            self._last_sampling_error = failure
            return
        self._last_sampling_error = None

        previous = self.snapshot
        self.snapshot = snapshot
        if previous is not None and snapshot.monitoring_paused != previous.monitoring_paused:
            paused = snapshot.monitoring_paused
            self._mark(
                snapshot.timestamp,
                TimelineKind.PAUSED if paused else TimelineKind.RESUMED,
                "paused" if paused else "resumed",
            )

        if not snapshot.monitoring_paused:
            self._sample_count += 1
            for key in HISTORY_KEYS:
                value = float(snapshot.totals.get(key, 0.0))
                self._history[key].append((snapshot.timestamp, value))
                self._peaks[key] = max(self._peaks[key], value)
            if snapshot.handle_types is not None:
                self._handle_types = dict(snapshot.handle_types)
            self._track_coverage(snapshot, first=previous is None)

        if severity_rank(snapshot.severity) > severity_rank(self._worst_severity):
            self._worst_severity = snapshot.severity
        for alert in snapshot.alerts:
            self._last_alert_by_resource[alert.resource] = alert
            worst = self._worst_by_resource.get(alert.resource, LeakSeverity.NONE)
            if severity_rank(alert.severity) > severity_rank(worst):
                self._worst_by_resource[alert.resource] = alert.severity

        if previous and snapshot.process_alive is False and previous.process_alive:
            code = self.manager.exit_code
            suffix = f" with code {code}" if code is not None else ""
            self._append_event(f"Target process exited{suffix}", "warn")

        if (
            previous is not None
            and previous.warming_up
            and not snapshot.warming_up
            and not snapshot.monitoring_paused
        ):
            self._log_warmup_complete(snapshot)

        if not snapshot.monitoring_paused:
            self._log_lifecycle(snapshot)
            self._log_signal_transitions(snapshot)

    @property
    def process_counts(self) -> ProcessCounts | None:
        """Live processes and start/exit/restart totals from the latest sample."""
        return self.snapshot.process_counts if self.snapshot is not None else None

    @property
    def exited_processes(self) -> tuple[ProcessSummary, ...]:
        """Summaries of the most recent processes that left the tree."""
        return self.snapshot.exited_processes if self.snapshot is not None else ()

    def _log_lifecycle(self, snapshot: MetricsSnapshot) -> None:
        """Log process starts, exits and restarts; coalesce bursts of one kind.

        The root's exit is left to the target-exit message.
        """
        groups: dict[LifecycleKind, list[LifecycleEvent]] = {}
        marks: dict[LifecycleKind, list[LifecycleEvent]] = {}
        for event in snapshot.lifecycle:
            self._lifecycle_history.append(event)
            marks.setdefault(event.kind, []).append(event)
            if event.kind == LifecycleKind.EXITED and event.pid == snapshot.root_pid:
                continue
            groups.setdefault(event.kind, []).append(event)
        for kind, events in marks.items():
            self._mark_lifecycle(snapshot.timestamp, kind, events)
        for kind, events in groups.items():
            level = "warn" if kind == LifecycleKind.RESTARTED else "info"
            if len(events) <= LIFECYCLE_DETAIL_LIMIT:
                for event in events:
                    self._append_event(self._lifecycle_line(event), level)
                continue
            names: dict[str, int] = {}
            for event in events:
                names[event.name] = names.get(event.name, 0) + 1
            shown = ", ".join(
                f"{name} x{count}" if count > 1 else name
                for name, count in sorted(names.items(), key=lambda item: -item[1])[:3]
            )
            more = ", ..." if len(names) > 3 else ""
            self._append_event(f"{len(events)} processes {kind.value} ({shown}{more})", level)

    def _lifecycle_line(self, event: LifecycleEvent) -> str:
        who = f"{event.name} PID {event.pid}"
        if event.kind == LifecycleKind.RESTARTED:
            return f"Process restarted: {who} replaces PID {event.replaces_pid}"
        return f"Process {event.kind.value}: {who}"

    _LIFECYCLE_MARKS = {
        LifecycleKind.STARTED: TimelineKind.STARTED,
        LifecycleKind.EXITED: TimelineKind.EXITED,
        LifecycleKind.RESTARTED: TimelineKind.RESTARTED,
    }

    def _mark_lifecycle(
        self, stamp: float, kind: LifecycleKind, events: list[LifecycleEvent]
    ) -> None:
        """One mark per process, or one for a burst, so churn cannot flood the timeline."""
        mark = self._LIFECYCLE_MARKS[kind]
        if len(events) > LIFECYCLE_DETAIL_LIMIT:
            self._mark(stamp, mark, f"{len(events)} processes {kind.value}")
            return
        for event in events:
            self._mark(stamp, mark, f"{event.name} PID {event.pid} {kind.value}", pid=event.pid)

    def _mark(self, stamp: float, kind: TimelineKind, label: str, **details: Any) -> TimelineMark:
        mark = TimelineMark(timestamp=stamp, kind=kind, label=label, **details)
        self._timeline.append(mark)
        return mark

    @property
    def timeline(self) -> tuple[TimelineMark, ...]:
        """Recent chart annotations, oldest first (bounded)."""
        return tuple(self._timeline)

    @property
    def markers(self) -> tuple[TimelineMark, ...]:
        """User markers added with ``add_marker``, oldest first."""
        return tuple(self._markers)

    def add_marker(self, label: str | None = None) -> TimelineMark | None:
        """Mark the current moment (``marker N``) in the event log, timeline and report.

        A marker records when something happened, such as starting a
        workload; it establishes timing only, not cause. Returns ``None``
        once ``MARKER_LIMIT`` markers exist.
        """
        if len(self._markers) >= self.MARKER_LIMIT:
            self._append_event(f"Marker limit of {self.MARKER_LIMIT} reached", "warn")
            return None
        self._marker_count += 1
        index = self._marker_count
        stamp = time.time()
        text = label or f"marker {index}"
        mark = self._mark(stamp, TimelineKind.MARKER, text, index=index)
        self._markers.append(mark)
        offset = (
            f" at +{self._format_duration(max(0.0, stamp - self._started_at))}"
            if self._started_at is not None
            else ""
        )
        self._append_event(f"Marker {index}: {text}{offset}", "info")
        return mark

    def lifecycle_for(self, pid: int, create_time: float | None = None) -> tuple[LifecycleEvent, ...]:
        """Recent starts, exits and restarts involving one process, oldest first.

        A restart that replaced ``pid`` counts too. With ``create_time``,
        events of an earlier process that reused the PID are left out.
        """
        matches = []
        for event in self._lifecycle_history:
            if event.replaces_pid == pid:
                matches.append(event)
                continue
            if event.pid != pid:
                continue
            if (
                create_time is not None
                and event.create_time is not None
                and abs(event.create_time - create_time) > 1e-3
            ):
                continue
            matches.append(event)
        return tuple(matches)

    def _markers_record(self) -> list[dict[str, Any]]:
        start = self._started_at
        return [
            {
                "index": mark.index,
                "label": mark.label,
                "timestamp": mark.timestamp,
                "time": iso_timestamp(mark.timestamp),
                "elapsed_seconds": round(mark.timestamp - start, 3) if start else None,
            }
            for mark in self._markers
        ]

    def attribution_text(self, alert: LeakAlert) -> str | None:
        """Where an alert's growth came from, or ``None`` without attribution.

        For example ``mostly worker.exe PID 1234`` or ``mostly new worker
        processes``; the detector made the ranking, this only words it.
        """
        top = alert.top_contributor
        if top is None:
            return None
        if alert.mostly_new_processes:
            return "mostly new worker processes"
        who = f"{top.name} PID {top.pid}"
        if top.exited:
            who += ", exited"
        return f"mostly {who}" if top.share > 0.5 else f"largest {who} ({top.share:.0%})"

    def _track_coverage(self, snapshot: MetricsSnapshot, first: bool) -> None:
        """Count partial samples and log processes that become (un)readable.

        A snapshot with no live process has nothing to measure, so it is
        not counted as missing coverage. The first sample is not logged
        here: the start-up messages already report what could not be read.
        """
        coverage = snapshot.coverage
        if not coverage:
            return
        live = [coverage[m] for m in SIGNAL_METRICS if m in coverage and coverage[m].total > 0]
        if live:
            self._coverage_samples += 1
        for item in live:
            if item.measured < item.total:
                self._partial_samples[item.metric] += 1
            if item.measured == 0:
                self._unavailable_samples[item.metric] += 1

        names = {process.pid: process.name for process in snapshot.processes}
        unreadable = {
            pid for m in SIGNAL_METRICS if m in coverage for pid in coverage[m].unmeasured_pids
        }
        for pid in sorted(unreadable):
            if pid not in self._unreadable_pids and len(self._unreadable_pids) < UNREADABLE_PID_LIMIT:
                self._unreadable_pids[pid] = names.get(pid, "")
        if not first:
            for pid in sorted(unreadable - self._current_unreadable):
                self._append_event(
                    f"Cannot read PID {pid}; its counters are left out of the totals "
                    "and detection restarts its windows",
                    "warn",
                )
                self._mark(snapshot.timestamp, TimelineKind.ACCESS_LOST,
                           f"PID {pid} unreadable", pid=pid)
            for pid in sorted(self._current_unreadable - unreadable):
                if pid in names:
                    self._append_event(
                        f"PID {pid} is readable again; detection restarts its windows", "info"
                    )
                    self._mark(snapshot.timestamp, TimelineKind.ACCESS_REGAINED,
                               f"PID {pid} readable", pid=pid)
        self._current_unreadable = unreadable

    def _coverage_record(self) -> dict[str, Any]:
        """Session-wide coverage of the signal metrics, for reports."""
        return {
            "samples": self._coverage_samples,
            "partial": self.partial_coverage,
            "metrics": {
                metric: {
                    "partial_samples": self._partial_samples[metric],
                    "unavailable_samples": self._unavailable_samples[metric],
                }
                for metric in SIGNAL_METRICS
            },
            "unreadable_pids": sorted(self._unreadable_pids),
            "latest": coverage_to_dict(self.snapshot.coverage) if self.snapshot else None,
        }

    def _outcome_record(self) -> dict[str, Any]:
        """Target, measurement, and monitor outcomes, kept apart for scripts."""
        if self.attached:
            state = "left_running" if self._left_running else "exited"
        elif self._stopped_target:
            state = "stopped"
        elif self.manager.exit_code is not None:
            state = "exited"
        else:
            state = "running" if self.manager.is_running else "unknown"
        return {
            "target": {
                "state": state,
                "exit_code": None if self._stopped_target else self.manager.exit_code,
                "failed": self.target_failed,
                "description": self.outcome_text(),
            },
            "measurement": {
                "result": self.measurement_outcome,
                "detection_ran": self.conclusive,
                "partial_coverage": self.partial_coverage,
                "reason": None if self.conclusive else self.inconclusive_reason(),
                "coverage": self.coverage_text(),
            },
            "monitor_errors": list(self._monitor_errors),
        }

    def _log_warmup_complete(self, snapshot: MetricsSnapshot) -> None:
        baseline = self.detector.warmup_baseline or snapshot.totals
        sep = self.glyphs.sep
        self._mark(snapshot.timestamp, TimelineKind.WARMUP_END, "warm-up complete")
        self._append_event(
            "Warm-up complete; leak detection started from a baseline of "
            f"{self._format_bytes(baseline.get('private_bytes', 0.0))} private {sep} "
            f"{int(baseline.get('handles', 0.0)):,} handles {sep} "
            f"{int(baseline.get('threads', 0.0)):,} threads",
            "ok",
        )

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
                self._mark(snapshot.timestamp, TimelineKind.CLEARED, f"{resource.value} cleared",
                           resource=resource, severity=current)
            else:
                self._mark(
                    snapshot.timestamp,
                    TimelineKind.SIGNAL,
                    f"{resource.value} {current.value.upper()}",
                    resource=resource,
                    severity=current,
                )
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
            # Say which counter grew: the memory card also lists RSS.
            amount = f"{self._format_bytes(delta)} private"
            rate = f"{self._format_bytes(alert.slope_per_second)}/s"
        else:
            amount = f"{delta:,.0f}"
            rate = f"{alert.slope_per_second:,.1f}/s"
        summary = f"+{amount} in {alert.window_seconds:.0f}s"
        attribution = self.attribution_text(alert)
        if attribution:
            summary += f", {attribution}"
        summary += f" {self.glyphs.sep} {rate}"
        types = describe_type_growth(alert.growth_by_type or {})
        return f"{summary} {self.glyphs.sep} {types}" if types else summary

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------

    def export_log(self) -> Path:
        """Write a JSON/HTML pair under its target filename and creation date.

        Every export gets a fresh UUID so repeated exports never overwrite
        an earlier report. Returns the JSON path. The HTML report is best
        effort: if it cannot be rendered or written, the JSON report still
        stands and the failure is logged.
        """
        report = self.build_report()
        directory = report_directory(report, self.report_dir)
        path = write_json_report(report, directory)
        self._last_report = path
        self._last_report_id = report["report_id"]
        self._last_report_data = report
        self._last_html_report = None
        try:
            self._last_html_report = write_html_report(report, directory)
        except Exception as exc:  # never lose the JSON report to a rendering bug
            self._append_event(f"HTML report failed: {exc}", "error")
            self._note_monitor_error(f"HTML report failed: {exc}")
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
            "settings": self._settings_record(),
            "experiment": self._experiment_record(),
            "warmup": self._warmup_record(),
            "detection_ran": self.detector.detection_started,
            "coverage": self._coverage_record(),
            "outcome": self._outcome_record(),
            "processes": self._processes_record(),
            "session_facts": self._facts_record(),
            # User markers: when the user said something happened (timing only).
            "markers": self._markers_record(),
            # Samples taken, and how many the snapshot list skips between
            # entries once a long session has been thinned (1: none).
            "sample_count": self._sample_count,
            "snapshot_stride": self._snapshot_history.stride,
            "snapshots": [snapshot_to_dict(snapshot) for snapshot in self._snapshot_history],
        }

    @property
    def facts(self) -> SessionFacts:
        """Exact session-wide summaries, independent of the thinned chart history."""
        return self._facts

    @property
    def diagnostics(self) -> MonitorDiagnostics | None:
        """The detector's latest account of its own sampling, if any."""
        return self._diagnostics

    def freshness_text(self, now: float | None = None) -> str | None:
        """How fresh and how costly sampling is, e.g. ``sample 0.4s ago, 12 ms, cadence 502 ms``.

        The age is of the last good sample, so it grows while samples fail
        or the monitor stalls. ``None`` before the first sample.
        """
        snapshot = self.snapshot
        if snapshot is None:
            return None
        sep = f" {self.glyphs.sep} "
        age = max(0.0, (time.time() if now is None else now) - snapshot.timestamp)
        parts = [f"sample {age:.1f}s ago"]
        diagnostics = self._diagnostics
        if diagnostics is not None:
            if diagnostics.collection_ms is not None:
                parts.append(f"{diagnostics.collection_ms:.0f} ms")
            if diagnostics.actual_interval_ms is not None:
                parts.append(f"cadence {diagnostics.actual_interval_ms:.0f} ms")
            else:
                parts.append(f"every {diagnostics.requested_interval_ms} ms")
            if diagnostics.failed_samples:
                parts.append(f"{diagnostics.failed_samples:,} failed")
            if diagnostics.dropped_snapshots:
                parts.append(f"{diagnostics.dropped_snapshots:,} dropped")
        else:
            parts.append(f"every {snapshot.sample_interval_ms} ms")
        return sep.join(parts)

    def monitor_cost_text(self) -> str | None:
        """Session-wide cadence and observer cost, or ``None`` before any was measured.

        For example ``cadence 502 ms mean (500 requested) · collection 12 ms
        mean, 30 ms max · monitor CPU 1.2% mean``.
        """
        monitor = self._facts.to_dict()["monitor"]
        sep = f" {self.glyphs.sep} "
        parts = []
        interval = monitor["interval_ms"]["mean"]
        if interval is not None:
            parts.append(
                f"cadence {interval:.0f} ms mean ({monitor['requested_interval_ms']} requested)"
            )
        collection = monitor["collection_ms"]
        if collection["mean"] is not None:
            parts.append(
                f"collection {collection['mean']:.0f} ms mean, {collection['max']:.0f} ms max"
            )
        cpu = monitor["monitor_cpu_percent"]["mean"]
        if cpu is not None:
            parts.append(f"monitor CPU {cpu:.1f}% mean")
        if monitor["dropped_snapshots"]:
            parts.append(f"{monitor['dropped_snapshots']:,} dropped")
        return sep.join(parts) or None

    def chart_thinning_text(self) -> str | None:
        """``chart thinned 1:N, summary facts exact`` once history was thinned, else ``None``."""
        stride = self._snapshot_history.stride
        if stride <= 1:
            return None
        return f"report chart thinned 1:{stride}; peaks, signals and counts stay exact"

    @property
    def chart_stride(self) -> int:
        """How many samples each stored chart point stands for (1: none thinned)."""
        return self._snapshot_history.stride

    def _facts_record(self) -> dict[str, Any]:
        """Session facts plus coverage and how much the stored chart was thinned."""
        record = self._facts.to_dict()
        record["coverage"] = {
            "samples": self._coverage_samples,
            "partial_samples": {m: self._partial_samples[m] for m in SIGNAL_METRICS},
            "unavailable_samples": {m: self._unavailable_samples[m] for m in SIGNAL_METRICS},
        }
        record["chart"] = {
            "stride": self._snapshot_history.stride,
            "stored": len(self._snapshot_history),
            "seen": self._snapshot_history.seen,
        }
        return record

    def _processes_record(self) -> dict[str, Any]:
        """Process counts and per-identity summaries, live and exited, for reports."""
        snapshot = self.snapshot
        counts = snapshot.process_counts if snapshot is not None else None
        return {
            "counts": None
            if counts is None
            else {
                "live": counts.live,
                "started": counts.started,
                "exited": counts.exited,
                "restarted": counts.restarted,
            },
            "live": [
                process_summary_to_dict(item)
                for item in (snapshot.process_summaries if snapshot else ())
            ],
            "exited": [process_summary_to_dict(item) for item in self.exited_processes],
        }

    def _warmup_record(self) -> dict[str, Any] | None:
        """Warm-up length, whether it finished, and the baseline it produced."""
        detector = self.detector
        if not detector.warmup_seconds:
            return None
        completed_at = detector.warmup_completed_at
        return {
            "seconds": detector.warmup_seconds,
            "completed": not detector.warming_up,
            "completed_at": iso_timestamp(completed_at) if completed_at else None,
            "baseline": dict(detector.warmup_baseline) if detector.warmup_baseline else None,
        }

    def _experiment_record(self) -> dict[str, Any]:
        """Workload labels, sampling and environment, with arguments redacted.

        Comparisons read this to tell equivalent experiments from runs that
        only share a target; see ``src/experiment.py`` for what is redacted.
        """
        detector = self.detector
        return experiment_record(
            self.settings.experiment,
            args=None if self.attached else self.manager.args,
            mode="attach" if self.attached else "launch",
            duration_seconds=self._uptime() if self._started_at else None,
            sample_interval_ms=detector.sample_interval_ms,
            windows_seconds=detector.window_seconds,
            warmup_seconds=detector.warmup_seconds,
            timeout_seconds=self.timeout,
            budgets=self.settings.budgets,
            version=self.VERSION,
        )

    def _settings_record(self) -> dict[str, Any]:
        """The detector configuration in effect, for reports and comparisons."""
        detector = self.detector
        return {
            "profile": self.settings.profile,
            "config_path": str(self.settings.source) if self.settings.source else None,
            "sample_interval_ms": detector.sample_interval_ms,
            "windows_seconds": list(detector.window_seconds),
            "warmup_seconds": detector.warmup_seconds,
            "thresholds": {
                resource.value: {
                    "critical_delta": limits.critical_delta,
                    "critical_slope": limits.critical_slope,
                }
                for resource, limits in detector.thresholds.items()
            },
            "timeout_seconds": self.timeout,
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
