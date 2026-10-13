"""Background resource sampling and leak detection heuristics."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Iterable

import psutil

from .events import (
    LeakAlert,
    LeakResource,
    LeakSeverity,
    MetricsSnapshot,
    MetricsStream,
    ProcessMetrics,
)
from .handle_types import (
    CATEGORIES,
    HandleClassifier,
    HandleCounts,
    describe_type_growth,
    total_counts,
)
from .process_runner import ProcessManager
from .stats import least_squares_slope

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_INTERVAL_MS = 500
DEFAULT_WINDOWS_SECONDS = (10.0, 30.0, 60.0)
DEFAULT_HANDLE_TYPES_INTERVAL = 2.0
# Back off handle classification so it stays near 5% of one core.
_CLASSIFY_COST_FACTOR = 20.0


@dataclass
class _SamplePoint:
    timestamp: float
    value: float


@dataclass
class _ResourceSeries:
    name: str
    resource: LeakResource
    points: deque[_SamplePoint]

    def add(self, timestamp: float, value: float, max_age_seconds: float) -> None:
        self.points.append(_SamplePoint(timestamp, value))
        cutoff = timestamp - max_age_seconds
        while self.points and self.points[0].timestamp < cutoff:
            self.points.popleft()

    def points_in_window(self, timestamp: float, window_seconds: float) -> list[_SamplePoint]:
        cutoff = timestamp - window_seconds
        return [point for point in self.points if point.timestamp >= cutoff]


class LeakDetector:
    """
    Sample process resources on a background thread and publish snapshots.

    Leak heuristics use sliding windows to detect monotonic growth in memory
    and handle counts, producing severity-scored alerts for the UI layer.
    """

    def __init__(
        self,
        process_manager: ProcessManager,
        stream: MetricsStream | None = None,
        sample_interval_ms: int = DEFAULT_SAMPLE_INTERVAL_MS,
        window_seconds: Iterable[float] = DEFAULT_WINDOWS_SECONDS,
        handle_types_interval: float = DEFAULT_HANDLE_TYPES_INTERVAL,
        classifier: HandleClassifier | None = None,
    ) -> None:
        self.process_manager = process_manager
        self.stream = stream or MetricsStream()
        self.sample_interval_ms = sample_interval_ms
        self.handle_types_interval = handle_types_interval
        self.classifier = classifier or HandleClassifier()
        self.window_seconds = tuple(sorted(window_seconds))
        self.max_window_seconds = max(self.window_seconds, default=60.0)

        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cpu_baselines: dict[int, tuple[float, float]] = {}
        self._io_baselines: dict[int, tuple[float, int, int]] = {}

        self._memory_series = _ResourceSeries("memory", LeakResource.MEMORY, deque())
        self._handle_series = _ResourceSeries("handles", LeakResource.HANDLES, deque())
        self._thread_series = _ResourceSeries("threads", LeakResource.THREADS, deque())
        self._last_processes: tuple[ProcessMetrics, ...] = ()
        self._last_totals: dict[str, float] | None = None
        self._last_alerts: tuple[LeakAlert, ...] = ()
        self._last_severity = LeakSeverity.NONE
        self._next_classification = 0.0
        self._handle_type_history: deque[tuple[float, HandleCounts]] = deque()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_paused(self) -> bool:
        return self._pause_event.is_set()

    def start(self) -> None:
        with self._lifecycle_lock:
            if self.is_running:
                return
            # A previous sampling operation may still be unwinding after a
            # timed-out stop. Do not clear its event and create an overlap.
            if self._thread is not None and self._thread.is_alive():
                logger.warning("Leak detector stop is still in progress")
                return
            self._stop_event.clear()
            self._pause_event.clear()
            self._thread = threading.Thread(
                target=self._sample_loop,
                name="LeakDetector",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop_event.set()
            thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        with self._lifecycle_lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None

    def pause(self) -> None:
        self._pause_event.set()

    def resume(self) -> None:
        self._pause_event.clear()

    def sample_once(self) -> MetricsSnapshot:
        """Collect a single snapshot synchronously (useful for tests/UI ticks)."""
        return self._collect_snapshot()

    def _sample_loop(self) -> None:
        interval = self.sample_interval_ms / 1000.0
        while not self._stop_event.wait(interval):
            try:
                if self._pause_event.is_set():
                    snapshot = self._build_paused_snapshot()
                else:
                    snapshot = self._collect_snapshot()
            except Exception as exc:
                logger.exception("Resource sampling failed")
                snapshot = self._build_error_snapshot(str(exc))
            self.stream.publish(snapshot)

    def _build_error_snapshot(self, error: str) -> MetricsSnapshot:
        return MetricsSnapshot(
            timestamp=time.time(),
            root_pid=self.process_manager.root_pid,
            process_alive=self.process_manager.is_running,
            monitoring_paused=False,
            processes=(),
            totals={
                "rss_bytes": 0.0,
                "vms_bytes": 0.0,
                "private_bytes": 0.0,
                "handles": 0.0,
                "threads": 0.0,
                "cpu_percent": 0.0,
                "io_read_bytes_per_sec": 0.0,
                "io_write_bytes_per_sec": 0.0,
            },
            alerts=(),
            severity=LeakSeverity.NONE,
            sample_interval_ms=self.sample_interval_ms,
            metadata={"sampling_error": error},
        )

    def _build_paused_snapshot(self) -> MetricsSnapshot:
        return MetricsSnapshot(
            timestamp=time.time(),
            root_pid=self.process_manager.root_pid,
            process_alive=self.process_manager.is_running,
            monitoring_paused=True,
            processes=self._last_processes,
            totals=dict(self._last_totals or self._zero_totals()),
            alerts=self._last_alerts,
            severity=self._last_severity,
            sample_interval_ms=self.sample_interval_ms,
            metadata={"last_error": self.process_manager.last_error, "paused": True},
        )

    @staticmethod
    def _zero_totals() -> dict[str, float]:
        return {
            "rss_bytes": 0.0,
            "vms_bytes": 0.0,
            "private_bytes": 0.0,
            "handles": 0.0,
            "threads": 0.0,
            "cpu_percent": 0.0,
            "io_read_bytes_per_sec": 0.0,
            "io_write_bytes_per_sec": 0.0,
        }

    def _collect_snapshot(self) -> MetricsSnapshot:
        now = time.time()
        self.process_manager.refresh_process_tree()
        processes = self._sample_processes()
        totals = self._aggregate_totals(processes)
        processes, handle_types = self._classify_handles(processes)
        max_age = self.max_window_seconds * 1.5
        if handle_types is not None:
            self._handle_type_history.append((now, handle_types))
        while self._handle_type_history and self._handle_type_history[0][0] < now - max_age:
            self._handle_type_history.popleft()

        self._last_processes = tuple(processes)
        self._last_totals = dict(totals)
        self._memory_series.add(now, totals["private_bytes"], max_age)
        self._handle_series.add(now, totals["handles"], max_age)
        self._thread_series.add(now, totals["threads"], max_age)

        alerts = self._evaluate_leaks(now, totals)
        severity = self._max_severity(alerts)
        self._last_alerts = tuple(alerts)
        self._last_severity = severity

        return MetricsSnapshot(
            timestamp=now,
            root_pid=self.process_manager.root_pid,
            process_alive=self.process_manager.is_running,
            monitoring_paused=False,
            processes=tuple(processes),
            totals=totals,
            alerts=tuple(alerts),
            severity=severity,
            sample_interval_ms=self.sample_interval_ms,
            metadata={"tracked_pids": sorted(self.process_manager.tracked_pids)},
            handle_types=handle_types,
        )

    def _classify_handles(
        self, processes: list[ProcessMetrics]
    ) -> tuple[list[ProcessMetrics], HandleCounts | None]:
        """Attach handle-type counts when a classification pass is due.

        Classification reads the whole system handle table, so it runs every
        ``handle_types_interval`` seconds rather than every sample, and backs
        off further when a pass is slow. Processes whose metrics could not
        be read are left out so the type counts add up to the handle total.
        """
        clock = time.monotonic()
        if not self.classifier.available or clock < self._next_classification:
            return processes, None
        measured = {
            process.pid
            for process in processes
            if not process.access_denied and process.status != psutil.STATUS_DEAD
        }
        if not measured:
            return processes, None
        per_pid = self.classifier.classify(measured)
        cost = time.monotonic() - clock
        self._next_classification = clock + max(
            self.handle_types_interval, cost * _CLASSIFY_COST_FACTOR
        )
        if per_pid is None:
            return processes, None
        classified = [
            replace(process, handle_types=per_pid[process.pid])
            if process.pid in measured and process.pid in per_pid
            else process
            for process in processes
        ]
        totals = total_counts({pid: counts for pid, counts in per_pid.items() if pid in measured})
        return classified, totals

    def _handle_growth_since(self, since: float) -> dict[str, int] | None:
        """Per-category handle change from the first classification at or after ``since``."""
        history = self._handle_type_history
        if len(history) < 2:
            return None
        baseline = next((counts for stamp, counts in history if stamp >= since), None)
        latest = history[-1][1]
        if baseline is None or baseline is latest:
            return None
        growth = {
            category: latest[category] - baseline[category]
            for category in CATEGORIES
            if latest[category] != baseline[category]
        }
        return growth or None

    def _sample_processes(self) -> list[ProcessMetrics]:
        metrics: list[ProcessMetrics] = []
        for proc in self.process_manager.iter_psutil_processes():
            metrics.append(self._sample_process(proc))
        # Forget rate baselines of processes that left the tree, so a long
        # session with many short-lived children does not accumulate them.
        sampled = {process.pid for process in metrics}
        for baselines in (self._cpu_baselines, self._io_baselines):
            for pid in [pid for pid in baselines if pid not in sampled]:
                del baselines[pid]
        return metrics

    def _sample_process(self, proc: psutil.Process) -> ProcessMetrics:
        access_denied = False
        pid = proc.pid

        try:
            with proc.oneshot():
                name = proc.name()
                status = proc.status()
                mem = proc.memory_info()
                rss = int(mem.rss)
                vms = int(mem.vms)
                private_bytes = int(getattr(mem, "private", mem.rss))
                handles = self._safe_num_handles(proc)
                threads = proc.num_threads()
                cpu_percent = self._sample_cpu_percent(proc)
                io_read_bps, io_write_bps = self._sample_io_rates(proc)
        except psutil.AccessDenied:
            access_denied = True
            name = "access-denied"
            status = "unknown"
            rss = vms = private_bytes = handles = threads = 0
            cpu_percent = io_read_bps = io_write_bps = 0.0
        except psutil.NoSuchProcess:
            return ProcessMetrics(
                pid=pid,
                name="terminated",
                status=psutil.STATUS_DEAD,
                rss_bytes=0,
                vms_bytes=0,
                private_bytes=0,
                handles=0,
                threads=0,
                cpu_percent=0.0,
                io_read_bytes_per_sec=0.0,
                io_write_bytes_per_sec=0.0,
                access_denied=False,
            )
        except (psutil.Error, OSError) as exc:
            return ProcessMetrics(
                pid=pid,
                name="unavailable",
                status="unknown",
                rss_bytes=0,
                vms_bytes=0,
                private_bytes=0,
                handles=0,
                threads=0,
                cpu_percent=0.0,
                io_read_bytes_per_sec=0.0,
                io_write_bytes_per_sec=0.0,
                access_denied=isinstance(exc, psutil.AccessDenied),
                ppid=self.process_manager.parent_of(pid),
            )

        return ProcessMetrics(
            pid=pid,
            name=name,
            status=status,
            rss_bytes=rss,
            vms_bytes=vms,
            private_bytes=private_bytes,
            handles=handles,
            threads=threads,
            cpu_percent=cpu_percent,
            io_read_bytes_per_sec=io_read_bps,
            io_write_bytes_per_sec=io_write_bps,
            access_denied=access_denied,
            ppid=self.process_manager.parent_of(pid),
        )

    @staticmethod
    def _safe_num_handles(proc: psutil.Process) -> int:
        try:
            return int(proc.num_handles())
        except (AttributeError, OSError, psutil.AccessDenied, NotImplementedError):
            return 0

    def _sample_cpu_percent(self, proc: psutil.Process) -> float:
        """CPU used since the previous sample, as a percentage of one core.

        psutil's ``cpu_percent(interval=None)`` keeps its baseline on the
        ``Process`` object, and tracked processes are reopened for every
        sample, so it would always report zero. Keep a CPU-time baseline
        per PID instead; like psutil, the result can exceed 100% when a
        process uses several cores.
        """
        try:
            times = proc.cpu_times()
            used = float(times.user) + float(times.system)
        except (psutil.Error, OSError, AttributeError):
            return 0.0
        stamp = time.monotonic()
        baseline = self._cpu_baselines.get(proc.pid)
        self._cpu_baselines[proc.pid] = (stamp, used)
        if baseline is None:
            return 0.0
        elapsed = stamp - baseline[0]
        if elapsed <= 0:
            return 0.0
        return max(0.0, (used - baseline[1]) / elapsed * 100.0)

    def _sample_io_rates(self, proc: psutil.Process) -> tuple[float, float]:
        try:
            counters = proc.io_counters()
            read_bytes = int(counters.read_bytes)
            write_bytes = int(counters.write_bytes)
        except (psutil.Error, OSError, AttributeError):
            return 0.0, 0.0
        stamp = time.monotonic()
        baseline = self._io_baselines.get(proc.pid)
        self._io_baselines[proc.pid] = (stamp, read_bytes, write_bytes)
        if baseline is None:
            return 0.0, 0.0
        last_time, last_read, last_write = baseline
        elapsed = stamp - last_time
        if elapsed <= 0:
            return 0.0, 0.0
        read_bps = max(0.0, (read_bytes - last_read) / elapsed)
        write_bps = max(0.0, (write_bytes - last_write) / elapsed)
        return read_bps, write_bps

    @staticmethod
    def _aggregate_totals(processes: list[ProcessMetrics]) -> dict[str, float]:
        return {
            "rss_bytes": float(sum(p.rss_bytes for p in processes)),
            "vms_bytes": float(sum(p.vms_bytes for p in processes)),
            "private_bytes": float(sum(p.private_bytes for p in processes)),
            "handles": float(sum(p.handles for p in processes)),
            "threads": float(sum(p.threads for p in processes)),
            "cpu_percent": float(sum(p.cpu_percent for p in processes)),
            "io_read_bytes_per_sec": float(sum(p.io_read_bytes_per_sec for p in processes)),
            "io_write_bytes_per_sec": float(sum(p.io_write_bytes_per_sec for p in processes)),
        }

    def _evaluate_leaks(self, timestamp: float, totals: dict[str, float]) -> list[LeakAlert]:
        alerts: list[LeakAlert] = []
        series_map = {
            LeakResource.MEMORY: (self._memory_series, totals["private_bytes"]),
            LeakResource.HANDLES: (self._handle_series, totals["handles"]),
            LeakResource.THREADS: (self._thread_series, totals["threads"]),
        }

        for resource, (series, current_value) in series_map.items():
            alert = self._evaluate_series(
                series=series,
                resource=resource,
                timestamp=timestamp,
                current_value=current_value,
            )
            if alert is not None:
                alerts.append(alert)

        return alerts

    def _evaluate_series(
        self,
        series: _ResourceSeries,
        resource: LeakResource,
        timestamp: float,
        current_value: float,
    ) -> LeakAlert | None:
        best_alert: LeakAlert | None = None
        best_since = timestamp

        for window in self.window_seconds:
            points = series.points_in_window(timestamp, window)
            if len(points) < 4:
                continue
            covered = points[-1].timestamp - points[0].timestamp
            # Require most of the window to have elapsed. Counting nominal
            # 500 ms slots misses real windows when sampling takes extra time.
            if covered < window * 0.85:
                continue

            values = [point.value for point in points]
            times = [point.timestamp for point in points]
            slope = self._least_squares_slope(times, values)
            monotonic_ratio = self._monotonic_increase_ratio(values)
            drop_ratio = self._drop_ratio(values)
            baseline = values[0]
            delta = current_value - baseline

            if slope <= 0 or monotonic_ratio < 0.65:
                continue
            if drop_ratio > 0.15:
                continue

            severity = self._score_severity(resource, slope, delta, monotonic_ratio, window)
            if severity == LeakSeverity.NONE:
                continue

            message = (
                f"Potential {resource.value} leak: "
                f"+{_format_quantity(resource, delta)} over {window:.0f}s "
                f"(slope {_format_quantity(resource, slope, precise=True)}/s, "
                f"monotonic {monotonic_ratio:.0%})"
            )
            candidate = LeakAlert(
                resource=resource,
                severity=severity,
                message=message,
                slope_per_second=slope,
                window_seconds=window,
                current_value=current_value,
                baseline_value=baseline,
            )

            if best_alert is None or _SEVERITY_RANK[candidate.severity] > _SEVERITY_RANK[best_alert.severity]:
                best_alert = candidate
                best_since = times[0]

        if best_alert is not None and resource == LeakResource.HANDLES:
            growth = self._handle_growth_since(best_since)
            if growth:
                described = describe_type_growth(growth)
                best_alert = replace(
                    best_alert,
                    growth_by_type=growth,
                    message=f"{best_alert.message}; {described}" if described else best_alert.message,
                )
        return best_alert

    _least_squares_slope = staticmethod(least_squares_slope)

    @staticmethod
    def _monotonic_increase_ratio(values: list[float]) -> float:
        if len(values) < 2:
            return 0.0
        increases = sum(1 for prev, curr in zip(values, values[1:]) if curr >= prev)
        return increases / (len(values) - 1)

    @staticmethod
    def _drop_ratio(values: list[float]) -> float:
        if len(values) < 2:
            return 0.0
        peak = values[0]
        drops = 0
        for value in values[1:]:
            if value < peak:
                drops += 1
            peak = max(peak, value)
        return drops / (len(values) - 1)

    @staticmethod
    def _score_severity(
        resource: LeakResource,
        slope: float,
        delta: float,
        monotonic_ratio: float,
        window: float,
    ) -> LeakSeverity:
        thresholds = _RESOURCE_THRESHOLDS[resource]
        score = 0.0

        score += min(delta / thresholds["delta_critical"], 3.0)
        score += min(slope / thresholds["slope_critical"], 3.0)
        score += monotonic_ratio

        if window >= 60:
            score += 0.5
        elif window >= 30:
            score += 0.25

        if score >= 4.5:
            return LeakSeverity.CRITICAL
        if score >= 3.5:
            return LeakSeverity.HIGH
        if score >= 2.5:
            return LeakSeverity.MEDIUM
        if score >= 1.8:
            return LeakSeverity.LOW
        return LeakSeverity.NONE

    @staticmethod
    def _max_severity(alerts: list[LeakAlert]) -> LeakSeverity:
        if not alerts:
            return LeakSeverity.NONE
        return max(alerts, key=lambda alert: _SEVERITY_RANK[alert.severity]).severity


def _format_quantity(resource: LeakResource, value: float, precise: bool = False) -> str:
    """Render memory in binary units and counts as plain numbers."""
    if resource != LeakResource.MEMORY:
        return f"{value:,.2f}" if precise else f"{value:,.0f}"
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(amount) < 1024 or unit == "GB":
            return f"{amount:,.1f} {unit}"
        amount /= 1024
    return f"{amount:,.1f} GB"


_SEVERITY_RANK = {
    LeakSeverity.NONE: 0,
    LeakSeverity.LOW: 1,
    LeakSeverity.MEDIUM: 2,
    LeakSeverity.HIGH: 3,
    LeakSeverity.CRITICAL: 4,
}

_RESOURCE_THRESHOLDS = {
    LeakResource.MEMORY: {
        "delta_critical": 50 * 1024 * 1024,  # 50 MB
        "slope_critical": 5 * 1024 * 1024,  # 5 MB/s
    },
    LeakResource.HANDLES: {
        "delta_critical": 500,
        "slope_critical": 20,
    },
    LeakResource.THREADS: {
        "delta_critical": 50,
        "slope_critical": 2,
    },
}
