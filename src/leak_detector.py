"""Background resource sampling and leak detection heuristics."""

from __future__ import annotations

import logging
import math
import os
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
    DetectionCriteria,
    EvidenceState,
    HostPressure,
    LeakAlert,
    LeakResource,
    LeakSeverity,
    MetricCoverage,
    MetricsSnapshot,
    MetricsStream,
    MonitorDiagnostics,
    ProcessMetrics,
    ProcessSummary,
    ResourceEvidence,
    WindowEvaluation,
    WindowVerdict,
)
from .handle_types import (
    CATEGORIES,
    HandleClassifier,
    HandleCounts,
    describe_type_growth,
    total_counts,
)
from .process_history import ProcessHistory
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
# A judged window signals only with an upward trend, at least this share of
# steps not falling, and at most this share of samples below the running peak.
MIN_MONOTONIC_RATIO = 0.65
MAX_DROP_RATIO = 0.15
# Minimum heuristic score per severity tier, strongest first.
SEVERITY_SCORES: tuple[tuple[LeakSeverity, float], ...] = (
    (LeakSeverity.CRITICAL, 4.5),
    (LeakSeverity.HIGH, 3.5),
    (LeakSeverity.MEDIUM, 2.5),
    (LeakSeverity.LOW, 1.8),
)
DETECTION_CRITERIA = DetectionCriteria(
    min_points=MIN_WINDOW_POINTS,
    min_span_ratio=MIN_WINDOW_COVERAGE,
    min_monotonic_ratio=MIN_MONOTONIC_RATIO,
    max_drop_ratio=MAX_DROP_RATIO,
    severity_scores=SEVERITY_SCORES,
)
_ALL_METRICS = tuple(METRIC_KEYS)
# Back off handle classification so it stays near 5% of one core.
_CLASSIFY_COST_FACTOR = 20.0
# Collected samples the reported cadence averages over.
CADENCE_SAMPLES = 20


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
        if self.points and timestamp <= self.points[-1].timestamp:
            self.points.clear()
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
        self._cpu_baselines: dict[int, tuple[float, float, float]] = {}
        self._io_baselines: dict[int, tuple[float, float, int, int]] = {}

        self._memory_series = _ResourceSeries("memory", LeakResource.MEMORY, deque())
        self._handle_series = _ResourceSeries("handles", LeakResource.HANDLES, deque())
        self._thread_series = _ResourceSeries("threads", LeakResource.THREADS, deque())
        self._last_processes: tuple[ProcessMetrics, ...] = ()
        self._last_totals: dict[str, float] | None = None
        self._last_alerts: tuple[LeakAlert, ...] = ()
        self._last_severity = LeakSeverity.NONE
        self._last_coverage: dict[str, MetricCoverage] | None = None
        self._last_evidence: tuple[ResourceEvidence, ...] = ()
        # Each resource's window evaluations from the latest judged sample.
        self._last_windows: dict[LeakResource, tuple[WindowEvaluation, ...]] = {}
        self._next_classification = 0.0
        self._handle_type_history: deque[tuple[float, HandleCounts]] = deque()
        # Per-process histories reach back as far as the series do, so
        # growth can be attributed over any alert window.
        self._process_history = ProcessHistory(self.max_window_seconds * 1.5)
        self._last_summaries: tuple[ProcessSummary, ...] = ()
        # Monitor diagnostics: recent sample start times, failures, the last
        # handle classification, and the monitor's own CPU-time baseline.
        self._collect_starts: deque[float] = deque(maxlen=CADENCE_SAMPLES)
        self._failed_samples = 0
        self._last_timing: tuple[float, float] | None = None
        self._clock_adjustments = 0
        self._sampling_gaps = 0
        self._classified_at: float | None = None
        self._classify_cost_ms: float | None = None
        self._self_process: psutil.Process | None = None
        self._self_cpu_baseline: tuple[float, float] | None = None
        self._host_reading: HostPressure | None = None
        self._host_sampled_at: float | None = None
        self._host_cpu_primed = False
        self._host_cpu_thread: int | None = None

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
    def process_history(self) -> ProcessHistory:
        """Per-process histories; safe to read from other threads."""
        return self._process_history

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
                    if self._last_timing is not None:
                        self._restart_temporal_windows()
                    self._last_timing = None
                    # Paused time does not count toward the warm-up.
                    self._last_active_sample = None
                    # A pause is not a slow sample: restart the cadence.
                    self._collect_starts.clear()
                    snapshot = self._build_paused_snapshot()
                else:
                    snapshot = self._collect_snapshot()
            except Exception as exc:
                error = str(exc) or type(exc).__name__
                if error != last_error:
                    logger.exception("Resource sampling failed")
                last_error = error
                self._failed_samples += 1
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
            diagnostics=self._diagnostics(None),
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
            process_counts=self._process_history.counts,
            process_summaries=self._last_summaries,
            exited_processes=self._process_history.exited(),
            diagnostics=self._diagnostics(None),
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
        started = time.perf_counter()
        monotonic = time.monotonic()
        timing = self._check_timing(now, monotonic)
        self._collect_starts.append(monotonic)
        self.process_manager.refresh_process_tree()
        processes = self._sample_processes()
        lifecycle = self._process_history.observe(now, processes)
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
        self._last_windows = {}
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
        self._last_summaries = self._process_history.summaries()
        diagnostics = self._diagnostics((time.perf_counter() - started) * 1000.0)

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
            metadata={"tracked_pids": sorted(self.process_manager.tracked_pids), **timing},
            handle_types=handle_types,
            warming_up=warming_up,
            coverage=coverage,
            evidence=evidence,
            lifecycle=lifecycle,
            process_counts=self._process_history.counts,
            process_summaries=self._last_summaries,
            exited_processes=self._process_history.exited(),
            diagnostics=diagnostics,
        )

    def _restart_temporal_windows(self) -> None:
        for series in self._series().values():
            series.interrupt()
        self._handle_type_history.clear()
        self._process_history.restart_windows()
        self._last_windows = {}
        self._last_active_sample = None

    def _check_timing(self, now: float, monotonic: float) -> dict:
        previous, self._last_timing = self._last_timing, (now, monotonic)
        if previous is None:
            return {}
        wall_gap, sample_gap = now - previous[0], monotonic - previous[1]
        # The clocks are read together. A backwards/non-advancing wall clock,
        # or a step exceeding one second relative to monotonic time, changes
        # the meaning of every wall-clock detection/attribution window.
        adjusted = wall_gap <= 0 or sample_gap < 0 or abs(wall_gap - sample_gap) > 1.0
        # Missing more than the shortest window's uncovered allowance cannot
        # help fill it. Keep cadence diagnostics so the delay stays visible.
        gap_limit = self.sample_interval_ms / 1000 + self.window_seconds[0] * (1 - MIN_WINDOW_COVERAGE)
        gap = sample_gap > gap_limit
        if not adjusted and not gap:
            return {}
        self._clock_adjustments += int(adjusted)
        self._sampling_gaps += int(gap)
        self._restart_temporal_windows()
        return {"timing_reset": "clock_adjustment" if adjusted else "sampling_gap",
                "sample_gap_seconds": max(0.0, sample_gap),
                "clock_adjustment_seconds": wall_gap - sample_gap}

    def _diagnostics(self, collection_ms: float | None) -> MonitorDiagnostics:
        """The monitor's own cadence, cost and footprint for this snapshot.

        Includes the monitor footprint and cached host context. Host queries
        run at most once per five seconds and report their own collection cost.
        """
        starts = self._collect_starts
        interval = (
            (starts[-1] - starts[0]) / (len(starts) - 1) * 1000.0 if len(starts) >= 2 else None
        )
        age = (
            max(0.0, time.monotonic() - self._classified_at)
            if self._classified_at is not None
            else None
        )
        cpu, rss, private = self._sample_self()
        return MonitorDiagnostics(
            requested_interval_ms=self.sample_interval_ms,
            actual_interval_ms=interval,
            collection_ms=collection_ms,
            handle_types_age_seconds=age,
            handle_types_cost_ms=self._classify_cost_ms,
            failed_samples=self._failed_samples,
            dropped_snapshots=self.stream.dropped,
            monitor_cpu_percent=cpu,
            monitor_rss_bytes=rss,
            monitor_private_bytes=private,
            clock_adjustments=self._clock_adjustments,
            sampling_gaps=self._sampling_gaps,
            host=self._sample_host(refresh=collection_ms is not None),
        )

    HOST_INTERVAL_SECONDS = 5.0

    def _sample_host(self, *, refresh: bool = True) -> HostPressure | None:
        """Bound host context collection to one nonblocking pass per five seconds."""
        now = time.monotonic()
        if refresh and (self._host_sampled_at is None or
                        now - self._host_sampled_at >= self.HOST_INTERVAL_SECONDS):
            started = time.perf_counter()
            cpu = available = total = None
            try:
                value = float(psutil.cpu_percent(interval=None))
                thread = threading.get_ident()
                if (self._host_cpu_primed and self._host_cpu_thread == thread
                        and math.isfinite(value) and 0 <= value <= 100):
                    cpu = value
                self._host_cpu_primed = True
                self._host_cpu_thread = thread
            except (psutil.Error, OSError, AttributeError, ValueError):
                self._host_cpu_primed = False
                self._host_cpu_thread = None
            try:
                memory = psutil.virtual_memory()
                available, total = int(memory.available), int(memory.total)
                if not 0 <= available <= total or total <= 0:
                    available = total = None
            except (psutil.Error, OSError, AttributeError, ValueError, OverflowError):
                pass
            self._host_reading = HostPressure(
                sampled_at=time.time(), cpu_percent=cpu,
                available_memory_bytes=available, total_memory_bytes=total,
                collection_ms=max(0.0, (time.perf_counter() - started) * 1000),
            )
            self._host_sampled_at = now
        if self._host_reading is None or self._host_sampled_at is None:
            return None
        return replace(self._host_reading, age_seconds=max(0.0, now - self._host_sampled_at))

    def _sample_self(self) -> tuple[float | None, int | None, int | None]:
        """DrainTracer's own CPU % of one core since the last call, RSS, and private bytes."""
        try:
            if self._self_process is None:
                self._self_process = psutil.Process(os.getpid())
            proc = self._self_process
            times = proc.cpu_times()
            used = float(times.user) + float(times.system)
            memory = proc.memory_info()
        except (psutil.Error, OSError, AttributeError):
            return None, None, None
        stamp = time.monotonic()
        baseline, self._self_cpu_baseline = self._self_cpu_baseline, (stamp, used)
        cpu = None
        if baseline is not None and stamp > baseline[0]:
            cpu = max(0.0, (used - baseline[1]) / (stamp - baseline[0]) * 100.0)
        return cpu, int(memory.rss), self._private_memory(proc, memory)

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
            windows: tuple[WindowEvaluation, ...] = ()
            if warming_up:
                state = EvidenceState.WARMING_UP
            elif measured is None or measured.unavailable:
                state = EvidenceState.UNAVAILABLE
            else:
                windows = self._last_windows.get(resource, ())
                if alert is not None:
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
                    windows=windows,
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

        Classification reads the system handle table or Linux descriptor
        lists, so it runs every
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
            if not process.access_denied
            and process.measured("handles")
            and process.status != psutil.STATUS_DEAD
        }
        if not measured:
            return processes, None
        per_pid = self.classifier.classify(measured)
        cost = time.monotonic() - clock
        self._classify_cost_ms = cost * 1000.0
        self._next_classification = clock + max(
            self.handle_types_interval, cost * _CLASSIFY_COST_FACTOR
        )
        if per_pid is None:
            self._handle_type_history.clear()
            return processes, None
        self._classified_at = clock
        classified = [
            replace(process, handle_types=per_pid[process.pid])
            if process.pid in measured and process.pid in per_pid
            else process
            for process in processes
        ]
        # A protected /proc descriptor directory must not silently become a
        # zero in the tree's classified totals. Per-process results remain
        # useful, but an incomplete classification has no comparable total.
        totals = (
            total_counts({pid: per_pid[pid] for pid in measured})
            if measured <= per_pid.keys() else None
        )
        if totals is None:
            self._handle_type_history.clear()
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
                read_private = self._private_memory(proc, mem)
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
                create_time=self.process_manager.created_at(pid),
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
                create_time=self.process_manager.created_at(pid),
            )
        else:
            private_bytes = 0 if read_private is None else read_private
            handles = 0 if read_handles is None else read_handles
            cpu_percent = 0.0 if read_cpu is None else read_cpu
            io_read_bps, io_write_bps = rates if rates is not None else (0.0, 0.0)
            if read_private is None:
                unmeasured.append("private_bytes")
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
            create_time=self.process_manager.created_at(pid),
        )

    @staticmethod
    def _private_memory(proc: psutil.Process, memory: object) -> int | None:
        """Windows private committed bytes or Linux unique resident pages.

        RSS includes shared pages, so it must never stand in for private
        memory. Linux USS may be protected even when basic counters can be
        read; leave only this metric unmeasured in that case.
        """
        private = getattr(memory, "private", None)
        if private is not None:
            return int(private)
        try:
            # psutil 8 exposes USS through memory_footprint; older supported
            # versions provide the same field through memory_full_info.
            footprint = getattr(proc, "memory_footprint", None)
            details = footprint() if footprint is not None else proc.memory_full_info()
            return int(details.uss)
        except (AttributeError, OSError, psutil.AccessDenied, NotImplementedError):
            return None

    @staticmethod
    def _safe_num_handles(proc: psutil.Process) -> int | None:
        """Windows handles or Linux descriptors; unreadable counts stay unknown."""
        try:
            count = getattr(proc, "num_handles", None)
            return int(count() if count is not None else proc.num_fds())
        except (AttributeError, OSError, psutil.AccessDenied, NotImplementedError):
            return None

    def _sample_cpu_percent(self, proc: psutil.Process) -> float | None:
        """CPU used since the previous sample, as a percentage of one core.

        psutil's ``cpu_percent(interval=None)`` keeps its baseline on the
        ``Process`` object, and tracked processes are reopened for every
        sample, so it would always report zero. Keep a CPU-time baseline
        per process identity instead; like psutil, the result can exceed 100% when a
        process uses several cores. Returns ``None`` when the CPU times
        cannot be read.
        """
        try:
            created = proc.create_time()
            times = proc.cpu_times()
            used = float(times.user) + float(times.system)
        except (psutil.Error, OSError, AttributeError):
            self._cpu_baselines.pop(proc.pid, None)
            return None
        stamp = time.monotonic()
        baseline = self._cpu_baselines.get(proc.pid)
        self._cpu_baselines[proc.pid] = (created, stamp, used)
        if baseline is None or baseline[0] != created:
            return 0.0
        elapsed = stamp - baseline[1]
        if elapsed <= 0:
            return 0.0
        return max(0.0, (used - baseline[2]) / elapsed * 100.0)

    def _sample_io_rates(self, proc: psutil.Process) -> tuple[float, float] | None:
        """Read and write rates since the previous sample, or ``None`` when unreadable."""
        try:
            created = proc.create_time()
            counters = proc.io_counters()
            read_bytes = int(counters.read_bytes)
            write_bytes = int(counters.write_bytes)
        except (psutil.Error, OSError, AttributeError):
            self._io_baselines.pop(proc.pid, None)
            return None
        stamp = time.monotonic()
        baseline = self._io_baselines.get(proc.pid)
        self._io_baselines[proc.pid] = (created, stamp, read_bytes, write_bytes)
        if baseline is None or baseline[0] != created:
            return 0.0, 0.0
        _created, last_time, last_read, last_write = baseline
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

        self._last_windows = {}
        for resource, (series, current_value) in series_map.items():
            alert, windows = self._evaluate_windows(
                series=series,
                resource=resource,
                timestamp=timestamp,
                current_value=current_value,
            )
            self._last_windows[resource] = windows
            if alert is not None:
                alerts.append(alert)

        return alerts

    @property
    def criteria(self) -> DetectionCriteria:
        """The window rules this detector applies, for views that explain them."""
        return DETECTION_CRITERIA

    def _evaluate_series(
        self,
        series: _ResourceSeries,
        resource: LeakResource,
        timestamp: float,
        current_value: float,
    ) -> LeakAlert | None:
        return self._evaluate_windows(series, resource, timestamp, current_value)[0]

    def _evaluate_windows(
        self,
        series: _ResourceSeries,
        resource: LeakResource,
        timestamp: float,
        current_value: float,
    ) -> tuple[LeakAlert | None, tuple[WindowEvaluation, ...]]:
        """Judge every window; return the strongest alert and each window's evaluation.

        The evaluations record the exact values the checks used, so the
        evidence view can say why a window did or did not qualify.
        """
        best_alert: LeakAlert | None = None
        best_since = timestamp
        evaluations: list[WindowEvaluation] = []

        for window in self.window_seconds:
            points = series.eligible_points(timestamp, window)
            if points is None:
                evaluations.append(self._unjudged_window(series, timestamp, window))
                continue

            values = [point.value for point in points]
            times = [point.timestamp for point in points]
            slope = self._least_squares_slope(times, values)
            monotonic_ratio = self._monotonic_increase_ratio(values)
            drop_ratio = self._drop_ratio(values)
            baseline = values[0]
            delta = current_value - baseline
            judged = WindowEvaluation(
                window_seconds=window,
                verdict=WindowVerdict.QUALIFIED,
                points=len(points),
                span_seconds=times[-1] - times[0],
                baseline_value=baseline,
                current_value=current_value,
                slope_per_second=slope,
                monotonic_ratio=monotonic_ratio,
                drop_ratio=drop_ratio,
            )

            if slope <= 0 or monotonic_ratio < MIN_MONOTONIC_RATIO:
                verdict = (
                    WindowVerdict.NO_UPWARD_TREND if slope <= 0 else WindowVerdict.NOT_MONOTONIC
                )
                evaluations.append(replace(judged, verdict=verdict))
                continue
            if drop_ratio > MAX_DROP_RATIO:
                evaluations.append(replace(judged, verdict=WindowVerdict.TOO_MANY_DROPS))
                continue

            score = self._severity_score(
                resource, slope, delta, monotonic_ratio, window, self.thresholds
            )
            severity = self._severity_for_score(score)
            if severity == LeakSeverity.NONE:
                evaluations.append(
                    replace(judged, verdict=WindowVerdict.BELOW_THRESHOLDS, score=score)
                )
                continue
            evaluations.append(replace(judged, score=score, severity=severity))

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

        if best_alert is not None:
            best_alert = self._attribute(best_alert, best_since, timestamp)
        if best_alert is not None and resource == LeakResource.HANDLES:
            growth = self._handle_growth_since(best_since)
            if growth:
                described = describe_type_growth(growth)
                best_alert = replace(
                    best_alert,
                    growth_by_type=growth,
                    message=f"{best_alert.message}; {described}" if described else best_alert.message,
                )
        return best_alert, tuple(evaluations)

    @staticmethod
    def _unjudged_window(
        series: _ResourceSeries, timestamp: float, window: float
    ) -> WindowEvaluation:
        """A window still filling: how many points it holds and over how long."""
        points = series.points_in_window(timestamp, window)
        span = points[-1].timestamp - points[0].timestamp if len(points) >= 2 else 0.0
        verdict = (
            WindowVerdict.TOO_FEW_SAMPLES
            if len(points) < MIN_WINDOW_POINTS
            else WindowVerdict.SHORT_SPAN
        )
        return WindowEvaluation(
            window_seconds=window, verdict=verdict, points=len(points), span_seconds=span
        )

    def _attribute(self, alert: LeakAlert, since: float, until: float) -> LeakAlert:
        """Name the processes behind an alert's growth over its own window.

        The alert and its severity stand as judged on the totals; this only
        says where the growth came from, so growth from newly started
        workers is described as such instead of as a leak in one process.
        """
        result = self._process_history.attribute(alert.metric, since, until)
        if not result.contributors:
            return alert
        attributed = replace(
            alert,
            contributors=result.contributors,
            growth_from_new_processes=result.growth_from_new_processes,
            growth_within_existing=result.growth_within_existing,
        )
        if attributed.mostly_new_processes:
            count = result.new_processes
            noun = "process" if count == 1 else "processes"
            note = f"mostly new worker {noun} ({count} started in the window)"
        else:
            top = result.contributors[0]
            amount = _format_quantity(alert.resource, top.delta)
            who = f"{top.name} PID {top.pid}"
            note = (
                f"mostly {who} (+{amount})"
                if top.share > 0.5
                else f"largest: {who} (+{amount}, {top.share:.0%})"
            )
        return replace(attributed, message=f"{alert.message}; {note}")

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

    @classmethod
    def _score_severity(
        cls,
        resource: LeakResource,
        slope: float,
        delta: float,
        monotonic_ratio: float,
        window: float,
        thresholds: Mapping[LeakResource, ResourceThresholds] | None = None,
    ) -> LeakSeverity:
        return cls._severity_for_score(
            cls._severity_score(resource, slope, delta, monotonic_ratio, window, thresholds)
        )

    @staticmethod
    def _severity_score(
        resource: LeakResource,
        slope: float,
        delta: float,
        monotonic_ratio: float,
        window: float,
        thresholds: Mapping[LeakResource, ResourceThresholds] | None = None,
    ) -> float:
        """Heuristic score: growth and rate against the thresholds, plus steadiness."""
        limits = (thresholds or DEFAULT_THRESHOLDS)[resource]
        score = 0.0

        score += min(delta / limits.critical_delta, 3.0)
        score += min(slope / limits.critical_slope, 3.0)
        score += monotonic_ratio

        if window >= 60:
            score += 0.5
        elif window >= 30:
            score += 0.25
        return score

    @staticmethod
    def _severity_for_score(score: float) -> LeakSeverity:
        for severity, minimum in SEVERITY_SCORES:
            if score >= minimum:
                return severity
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
