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
    COVERAGE_METRICS,
    METRIC_KEYS,
    METRIC_LABELS,
    RESOURCE_METRICS,
    EvidenceState,
    LeakAlert,
    LeakResource,
    LeakSeverity,
    MetricCoverage,
    MetricsSnapshot,
    MetricsStream,
    ProcessMetrics,
    ResourceEvidence,
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
# A window is judged once it holds this many points spanning this share of it.
MIN_WINDOW_POINTS = 4
MIN_WINDOW_COVERAGE = 0.85
_ALL_METRICS = tuple(METRIC_KEYS)
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
    """Valid observations of one resource total since its measured set last changed.

    ``segment`` identifies the processes left out of the total (alive but
    unreadable). When it changes, the total starts or stops counting a
    process for a reason that has nothing to do with growth, so earlier
    points no longer compare and the series starts over.
    """

    name: str
    resource: LeakResource
    points: deque[_SamplePoint]
    segment: frozenset[int] | None = None

    def continue_segment(self, segment: frozenset[int]) -> bool:
        """Keep the points if ``segment`` matches; else clear them. Return whether kept."""
        if segment == self.segment:
            return True
        self.points.clear()
        self.segment = segment
        return False

    def interrupt(self) -> None:
        """Forget the points after an observation that could not be measured."""
        self.points.clear()
        self.segment = None

    def add(self, timestamp: float, value: float, max_age_seconds: float) -> None:
        self.points.append(_SamplePoint(timestamp, value))
        cutoff = timestamp - max_age_seconds
        while self.points and self.points[0].timestamp < cutoff:
            self.points.popleft()

    def points_in_window(self, timestamp: float, window_seconds: float) -> list[_SamplePoint]:
        cutoff = timestamp - window_seconds
        return [point for point in self.points if point.timestamp >= cutoff]

    def eligible_points(self, timestamp: float, window_seconds: float) -> list[_SamplePoint] | None:
        """The window's points when there are enough to judge it, else ``None``.

        Counting nominal sample slots would miss real windows when sampling
        takes extra time, so most of the window must have elapsed instead.
        """
        points = self.points_in_window(timestamp, window_seconds)
        if len(points) < MIN_WINDOW_POINTS:
            return None
        if points[-1].timestamp - points[0].timestamp < window_seconds * MIN_WINDOW_COVERAGE:
            return None
        return points


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
        self._last_coverage: dict[str, MetricCoverage] | None = None
        self._last_evidence: tuple[ResourceEvidence, ...] = ()
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

        Any resource counts. Until then no signal can be raised, so a clean result says nothing.
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
        # A persistent failure repeats every sample; log its traceback once.
        last_error: str | None = None
        while not self._stop_event.wait(max(0.0, next_tick - time.monotonic())):
            try:
                if self._pause_event.is_set():
                    # Paused time does not count toward the warm-up.
                    self._last_active_sample = None
                    snapshot = self._build_paused_snapshot()
                else:
                    snapshot = self._collect_snapshot()
            except Exception as exc:
                error = str(exc) or type(exc).__name__
                if error != last_error:
                    logger.exception("Resource sampling failed")
                last_error = error
                snapshot = self._build_error_snapshot(error)
            else:
                last_error = None
            try:
                self.stream.publish(snapshot)
            except Exception:
                logger.exception("Publishing a snapshot failed")
            next_tick = self._next_tick(next_tick, time.monotonic(), interval)

    @staticmethod
    def _next_tick(previous: float, now: float, interval: float) -> float:
        """The next sample time: on the cadence, but at least half an interval away."""
        return max(previous + interval, now + interval / 2)

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
            evidence=tuple(
                ResourceEvidence(resource=resource, state=EvidenceState.UNAVAILABLE)
                for resource in RESOURCE_METRICS
            ),
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
            coverage=self._last_coverage,
            evidence=tuple(
                replace(item, state=EvidenceState.STALE) for item in self._last_evidence
            ),
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
        coverage = self._coverage(processes)
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
            self._extend_series(now, totals, coverage, max_age)
            if not self._detection_started:
                self._detection_started = self._window_ready(now)
            alerts = self._evaluate_leaks(now, totals)
        severity = self._max_severity(alerts)
        evidence = self._evidence(now, warming_up, coverage, alerts)
        self._last_alerts = tuple(alerts)
        self._last_severity = severity
        self._last_coverage = coverage
        self._last_evidence = evidence

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
            coverage=coverage,
            evidence=evidence,
        )

    def _series(self) -> dict[LeakResource, _ResourceSeries]:
        return {
            LeakResource.MEMORY: self._memory_series,
            LeakResource.HANDLES: self._handle_series,
            LeakResource.THREADS: self._thread_series,
        }

    def _extend_series(
        self,
        now: float,
        totals: Mapping[str, float],
        coverage: Mapping[str, MetricCoverage],
        max_age: float,
    ) -> None:
        """Add each valid resource total to its series.

        An observation with no measured process is invalid: it breaks the
        series rather than adding a false zero. A change in which live
        processes are unreadable (access lost or regained, an unreadable
        process starting or exiting) starts a new segment, so a window
        never spans samples whose totals cover different processes and the
        change cannot pass for growth or a drop. Readable processes that
        start or exit stay in one segment: their effect on the total is real.
        """
        for resource, series in self._series().items():
            metric = RESOURCE_METRICS[resource]
            measured = coverage[metric]
            if measured.unavailable:
                series.interrupt()
                continue
            series.continue_segment(frozenset(measured.unmeasured_pids))
            series.add(now, totals[metric], max_age)

    def _window_ready(self, now: float) -> bool:
        """Whether any resource's shortest window passes the evaluation preconditions."""
        window = self.window_seconds[0]
        return any(
            series.eligible_points(now, window) is not None for series in self._series().values()
        )

    @staticmethod
    def _coverage(processes: Iterable[ProcessMetrics]) -> dict[str, MetricCoverage]:
        """How many live processes each covered metric was read from.

        Processes that already exited are left out: they no longer hold
        resources, so they are neither measured nor missing.
        """
        live = [process for process in processes if process.status != psutil.STATUS_DEAD]
        coverage = {}
        for metric in COVERAGE_METRICS:
            missing = tuple(sorted(p.pid for p in live if not p.measured(metric)))
            coverage[metric] = MetricCoverage(
                metric=metric,
                measured=len(live) - len(missing),
                total=len(live),
                unmeasured_pids=missing,
            )
        return coverage

    def _evidence(
        self,
        now: float,
        warming_up: bool,
        coverage: Mapping[str, MetricCoverage],
        alerts: Iterable[LeakAlert],
    ) -> tuple[ResourceEvidence, ...]:
        """What each resource's signal can say at this sample, for the front ends.

        The UI renders these states and never re-derives them from the totals.
        """
        by_resource = {alert.resource: alert for alert in alerts}
        needed_seconds = self.window_seconds[0] * MIN_WINDOW_COVERAGE
        evidence = []
        for resource, series in self._series().items():
            measured = coverage.get(RESOURCE_METRICS[resource])
            points = [] if warming_up else list(series.points)
            elapsed = points[-1].timestamp - points[0].timestamp if len(points) >= 2 else 0.0
            alert = by_resource.get(resource)
            window: float | None = None
            if warming_up:
                state = EvidenceState.WARMING_UP
            elif measured is None or measured.unavailable:
                state = EvidenceState.UNAVAILABLE
            elif alert is not None:
                state, window = EvidenceState.GROWTH, alert.window_seconds
            else:
                judged = [w for w in self.window_seconds if series.eligible_points(now, w)]
                state = EvidenceState.NO_GROWTH if judged else EvidenceState.COLLECTING
                window = judged[-1] if judged else None
            evidence.append(
                ResourceEvidence(
                    resource=resource,
                    state=state,
                    valid_samples=len(points),
                    needed_samples=MIN_WINDOW_POINTS,
                    elapsed_seconds=elapsed,
                    needed_seconds=needed_seconds,
                    evaluated_window_seconds=window,
                    severity=alert.severity if alert is not None else LeakSeverity.NONE,
                    coverage=measured,
                )
            )
        return tuple(evidence)

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
        """Read one process; counters that cannot be read are marked unmeasured.

        Unreadable counters keep a 0 in their field for compatibility, but
        ``unmeasured`` names them so totals, coverage and detection never
        treat that 0 as a real measurement.
        """
        access_denied = False
        pid = proc.pid
        unmeasured: list[str] = []

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
                read_handles = self._safe_num_handles(proc)
                threads = entry.threads if entry is not None else proc.num_threads()
                read_cpu = self._sample_cpu_percent(proc)
                rates = self._sample_io_rates(proc)
        except psutil.AccessDenied:
            access_denied = True
            name = "access-denied"
            status = "unknown"
            rss = vms = private_bytes = handles = threads = 0
            cpu_percent = io_read_bps = io_write_bps = 0.0
            unmeasured = list(_ALL_METRICS)
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
                unmeasured=_ALL_METRICS,
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
                unmeasured=_ALL_METRICS,
            )
        else:
            handles = 0 if read_handles is None else read_handles
            cpu_percent = 0.0 if read_cpu is None else read_cpu
            io_read_bps, io_write_bps = rates if rates is not None else (0.0, 0.0)
            if read_handles is None:
                unmeasured.append("handles")
            if read_cpu is None:
                unmeasured.append("cpu_percent")
            if rates is None:
                unmeasured += ["io_read_bytes_per_sec", "io_write_bytes_per_sec"]

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
            unmeasured=tuple(unmeasured),
        )

    @staticmethod
    def _safe_num_handles(proc: psutil.Process) -> int | None:
        """The handle count, or ``None`` when it cannot be read (never a false 0)."""
        try:
            return int(proc.num_handles())
        except (AttributeError, OSError, psutil.AccessDenied, NotImplementedError):
            return None

    def _sample_cpu_percent(self, proc: psutil.Process) -> float | None:
        """CPU used since the previous sample, as a percentage of one core.

        psutil's ``cpu_percent(interval=None)`` keeps its baseline on the
        ``Process`` object, and tracked processes are reopened for every
        sample, so it would always report zero. Keep a CPU-time baseline
        per PID instead; like psutil, the result can exceed 100% when a
        process uses several cores. Returns ``None`` when the CPU times
        cannot be read.
        """
        try:
            times = proc.cpu_times()
            used = float(times.user) + float(times.system)
        except (psutil.Error, OSError, AttributeError):
            return None
        stamp = time.monotonic()
        baseline = self._cpu_baselines.get(proc.pid)
        self._cpu_baselines[proc.pid] = (stamp, used)
        if baseline is None:
            return 0.0
        elapsed = stamp - baseline[0]
        if elapsed <= 0:
            return 0.0
        return max(0.0, (used - baseline[1]) / elapsed * 100.0)

    def _sample_io_rates(self, proc: psutil.Process) -> tuple[float, float] | None:
        """Read and write rates since the previous sample, or ``None`` when unreadable."""
        try:
            counters = proc.io_counters()
            read_bytes = int(counters.read_bytes)
            write_bytes = int(counters.write_bytes)
        except (psutil.Error, OSError, AttributeError):
            return None
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
        """Sum each metric over the processes it was measured for.

        Totals stay plain numbers for compatibility; the snapshot's
        ``coverage`` says when one leaves processes out.
        """
        return {
            key: float(sum(getattr(p, key) for p in processes if p.measured(key)))
            for key in METRIC_KEYS
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
            points = series.eligible_points(timestamp, window)
            if points is None:
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

            # Name the counter for memory: "memory" alone reads as RSS.
            subject = (
                f"{resource.value} ({METRIC_LABELS[RESOURCE_METRICS[resource]]})"
                if resource == LeakResource.MEMORY
                else resource.value
            )
            message = (
                f"Potential {subject} leak: "
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
