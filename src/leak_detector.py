"""Background resource sampling and leak detection heuristics."""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType

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
from .process_table import ProcessEntry, ProcessTable
from .stats import least_squares_slope

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_INTERVAL_MS = 500
DEFAULT_WINDOWS_SECONDS = (10.0, 30.0, 60.0)
DEFAULT_HANDLE_TYPES_INTERVAL = 2.0
MIB = 1024 * 1024
# Back off handle classification so it stays near 5% of one core.
_CLASSIFY_COST_FACTOR = 20.0


@dataclass(frozen=True)
class ResourceThresholds:
    """Growth a resource must show to count as critical.

    ``critical_delta`` is accumulated growth over a window and
    ``critical_slope`` a growth rate per second. They normalize the severity
    score rather than acting as hard limits.
    """

    critical_delta: float
    critical_slope: float

    def __post_init__(self) -> None:
        for name in ("critical_delta", "critical_slope"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a number, got {value!r}")
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be greater than zero, got {value!r}")


DEFAULT_THRESHOLDS: Mapping[LeakResource, ResourceThresholds] = MappingProxyType(
    {
        LeakResource.MEMORY: ResourceThresholds(critical_delta=50 * MIB, critical_slope=5 * MIB),
        LeakResource.HANDLES: ResourceThresholds(critical_delta=500, critical_slope=20),
        LeakResource.THREADS: ResourceThresholds(critical_delta=50, critical_slope=2),
    }
)


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
        thresholds: Mapping[LeakResource, ResourceThresholds] | None = None,
        warmup_seconds: float = 0.0,
    ) -> None:
        windows = tuple(sorted(set(float(window) for window in window_seconds)))
        if not windows or any(not math.isfinite(window) or window <= 0 for window in windows):
            raise ValueError("window_seconds needs at least one positive window")
        if not sample_interval_ms > 0:
            raise ValueError("sample_interval_ms must be greater than zero")
        if not math.isfinite(warmup_seconds) or warmup_seconds < 0:
            raise ValueError("warmup_seconds must be zero or a positive number of seconds")
        self.process_manager = process_manager
        self.stream = stream or MetricsStream()
        self.sample_interval_ms = sample_interval_ms
        self.handle_types_interval = handle_types_interval
        self.classifier = classifier or HandleClassifier()
        self._process_table = ProcessTable()
        self.window_seconds = windows
        self.max_window_seconds = windows[-1]
        # Resources without an override keep the defaults.
        self.thresholds: Mapping[LeakResource, ResourceThresholds] = MappingProxyType(
            {**DEFAULT_THRESHOLDS, **(thresholds or {})}
        )

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

        # Warm-up: sampling runs but leak heuristics wait until this much
        # active (unpaused) sampling time has passed.
        self.warmup_seconds = float(warmup_seconds)
        self._warmup_elapsed = 0.0
        self._last_active_sample: float | None = None
        self.warmup_completed_at: float | None = None
        self.warmup_baseline: dict[str, float] | None = None
        self._detection_started = False

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_paused(self) -> bool:
        return self._pause_event.is_set()

    @property
    def warming_up(self) -> bool:
        """Whether leak heuristics are still deferred by the warm-up."""
        return self._warmup_elapsed < self.warmup_seconds

    @property
    def warmup_remaining(self) -> float:
        """Active sampling seconds left before leak detection starts."""
        return max(0.0, self.warmup_seconds - self._warmup_elapsed)

    @property
    def detection_started(self) -> bool:
        """Whether the shortest window has ever held enough samples to judge.

        Until then no signal can be raised, so a clean result says nothing.
        """
        return self._detection_started

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
        """Sample on a steady cadence until stopped.

        Ticks are scheduled from the start of each sample, so the period
        matches the configured interval when sampling is cheap. When a
        sample runs long (a large tree), the next one still waits at least
        half an interval instead of sampling back to back.
        """
        interval = self.sample_interval_ms / 1000.0
        next_tick = time.monotonic() + interval
        while not self._stop_event.wait(max(0.0, next_tick - time.monotonic())):
            try:
                if self._pause_event.is_set():
                    # Paused time does not count toward the warm-up.
                    self._last_active_sample = None
                    snapshot = self._build_paused_snapshot()
                else:
                    snapshot = self._collect_snapshot()
            except Exception as exc:
                logger.exception("Resource sampling failed")
                snapshot = self._build_error_snapshot(str(exc) or type(exc).__name__)
            try:
                self.stream.publish(snapshot)
            except Exception:
                logger.exception("Publishing a snapshot failed")
            next_tick = max(next_tick + interval, time.monotonic() + interval / 2)

    def _build_error_snapshot(self, error: str) -> MetricsSnapshot:
        try:
            alive = self.process_manager.is_running
        except Exception:
            alive = False
        return MetricsSnapshot(
            timestamp=time.time(),
            root_pid=self.process_manager.root_pid,
            process_alive=alive,
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
            warming_up=self.warming_up,
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
        warming_up = self._advance_warmup(now, totals)
        alerts: list[LeakAlert] = []
        if not warming_up:
            # Series start empty after the warm-up, so initialization growth
            # never reaches a window.
            self._memory_series.add(now, totals["private_bytes"], max_age)
            self._handle_series.add(now, totals["handles"], max_age)
            self._thread_series.add(now, totals["threads"], max_age)
            if not self._detection_started:
                self._detection_started = self._window_ready(now)
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
            warming_up=warming_up,
        )

    def _window_ready(self, now: float) -> bool:
        """Whether the shortest window passes the evaluation preconditions."""
        window = self.window_seconds[0]
        points = self._memory_series.points_in_window(now, window)
        return len(points) >= 4 and points[-1].timestamp - points[0].timestamp >= window * 0.85

    def _advance_warmup(self, now: float, totals: dict[str, float]) -> bool:
        """Count active sampling time; return whether the warm-up continues.

        The totals of the first sample after the warm-up become the baseline
        that later growth is measured against.
        """
        clock = time.monotonic()
        if self._last_active_sample is not None:
            self._warmup_elapsed += clock - self._last_active_sample
        self._last_active_sample = clock
        if self.warming_up:
            return True
        if self.warmup_seconds > 0 and self.warmup_baseline is None:
            self.warmup_baseline = dict(totals)
            self.warmup_completed_at = now
        return False

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
        processes = self.process_manager.iter_psutil_processes()
        # One system snapshot answers thread counts and suspension for the
        # whole tree; psutil would enumerate every process once per call.
        table = self._process_table.snapshot([proc.pid for proc in processes]) if processes else None
        metrics = [
            self._sample_process(proc, table.get(proc.pid) if table else None)
            for proc in processes
        ]
        # Forget rate baselines of processes that left the tree, so a long
        # session with many short-lived children does not accumulate them.
        sampled = {process.pid for process in metrics}
        for baselines in (self._cpu_baselines, self._io_baselines):
            for pid in [pid for pid in baselines if pid not in sampled]:
                del baselines[pid]
        return metrics

    def _sample_process(
        self, proc: psutil.Process, entry: ProcessEntry | None = None
    ) -> ProcessMetrics:
        access_denied = False
        pid = proc.pid

        try:
            with proc.oneshot():
                name = proc.name()
                if entry is not None:
                    status = psutil.STATUS_STOPPED if entry.suspended else psutil.STATUS_RUNNING
                else:
                    status = proc.status()
                mem = proc.memory_info()
                rss = int(mem.rss)
                vms = int(mem.vms)
                private_bytes = int(getattr(mem, "private", mem.rss))
                handles = self._safe_num_handles(proc)
                threads = entry.threads if entry is not None else proc.num_threads()
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

            severity = self._score_severity(
                resource, slope, delta, monotonic_ratio, window, self.thresholds
            )
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
        thresholds: Mapping[LeakResource, ResourceThresholds] | None = None,
    ) -> LeakSeverity:
        limits = (thresholds or DEFAULT_THRESHOLDS)[resource]
        score = 0.0

        score += min(delta / limits.critical_delta, 3.0)
        score += min(slope / limits.critical_slope, 3.0)
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
