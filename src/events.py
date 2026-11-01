"""Event stream and structured data models for the leak detector UI layer."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from enum import Enum
from queue import Empty, Queue
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .session_facts import SessionFacts


class LeakSeverity(str, Enum):
    """Severity tier for a detected resource leak."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


_SEVERITY_ORDER = {
    LeakSeverity.NONE: 0,
    LeakSeverity.LOW: 1,
    LeakSeverity.MEDIUM: 2,
    LeakSeverity.HIGH: 3,
    LeakSeverity.CRITICAL: 4,
}


def severity_rank(severity: LeakSeverity) -> int:
    """Return a sortable rank where higher means more severe."""
    return _SEVERITY_ORDER[severity]


class LeakResource(str, Enum):
    """Resource category flagged by leak heuristics."""

    MEMORY = "memory"
    HANDLES = "handles"
    THREADS = "threads"


@dataclass(frozen=True)
class GrowthContributor:
    """One process's change in an alerted metric over the alert window.

    ``share`` is its part of all positive growth in that window.
    ``new_process`` marks a process started inside the window, whose whole
    value counts as growth; ``exited`` one that ended before the window did.
    """

    pid: int
    create_time: float | None
    name: str
    delta: float
    share: float
    new_process: bool = False
    exited: bool = False


@dataclass(frozen=True)
class LeakAlert:
    """A single leak heuristic result for one resource category.

    ``growth_by_type`` is set for handle alerts when handle types were
    classified during the window: the change per handle category between
    the first classification in the window and the latest one.

    ``contributors`` ranks the processes whose alerted metric grew over the
    same window, largest first. Growth is split into what processes that
    already ran at the window start added (``growth_within_existing``) and
    what processes started inside the window brought
    (``growth_from_new_processes``); both sum positive changes only.
    """

    resource: LeakResource
    severity: LeakSeverity
    message: str
    slope_per_second: float
    window_seconds: float
    current_value: float
    baseline_value: float
    growth_by_type: dict[str, int] | None = None
    contributors: tuple[GrowthContributor, ...] = ()
    growth_from_new_processes: float = 0.0
    growth_within_existing: float = 0.0

    @property
    def top_contributor(self) -> GrowthContributor | None:
        """The process that grew the alerted metric most over the window."""
        return self.contributors[0] if self.contributors else None

    @property
    def mostly_new_processes(self) -> bool:
        """Whether processes started inside the window account for most growth.

        Then the total grew because the tree added capacity (new workers),
        which is not by itself a leak inside any one process.
        """
        growth = self.growth_from_new_processes + self.growth_within_existing
        return growth > 0 and self.growth_from_new_processes > growth / 2

    @property
    def metric(self) -> str:
        """The totals key whose growth raised this alert.

        Memory alerts come from private bytes, not RSS, so views and reports
        name the metric explicitly instead of saying only "memory".
        """
        return RESOURCE_METRICS[self.resource]


# Per-process counters a sample can carry, named like the snapshot totals.
METRIC_KEYS = (
    "rss_bytes",
    "vms_bytes",
    "private_bytes",
    "handles",
    "threads",
    "cpu_percent",
    "io_read_bytes_per_sec",
    "io_write_bytes_per_sec",
)
# The totals series behind each resource's leak signal.
RESOURCE_METRICS: dict[LeakResource, str] = {
    LeakResource.MEMORY: "private_bytes",
    LeakResource.HANDLES: "handles",
    LeakResource.THREADS: "threads",
}
# Metrics whose coverage snapshots record: the signal inputs plus RSS,
# which the dashboard charts next to private bytes.
COVERAGE_METRICS = ("rss_bytes", "private_bytes", "handles", "threads")
# One name per memory counter everywhere it is shown. Private bytes are the
# committed memory a process alone owns and drive the memory signal; RSS is
# the working set (resident pages, shared ones included) and is context only.
METRIC_LABELS: dict[str, str] = {
    "private_bytes": "private bytes",
    "rss_bytes": "RSS (working set)",
    "handles": "handles",
    "threads": "threads",
}


class EvidenceState(str, Enum):
    """How much a resource's leak signal can say at one sample.

    The states keep "nothing found" apart from "nothing judged yet": only
    ``NO_GROWTH`` and ``GROWTH`` come from an evaluated window.
    """

    WARMING_UP = "warming_up"
    COLLECTING = "collecting"
    NO_GROWTH = "no_growth"
    GROWTH = "growth"
    UNAVAILABLE = "unavailable"
    STALE = "stale"


@dataclass(frozen=True)
class MetricCoverage:
    """How many live processes one metric could actually be read from.

    Unreadable counters are not zeros: the totals only add up the
    ``measured`` processes, so a partial total is a lower bound.
    """

    metric: str
    measured: int
    total: int
    unmeasured_pids: tuple[int, ...] = ()

    @property
    def partial(self) -> bool:
        """Some, but not all, live processes were measured."""
        return 0 < self.measured < self.total

    @property
    def unavailable(self) -> bool:
        """No live process could be measured, so the total means nothing."""
        return self.measured == 0


class WindowVerdict(str, Enum):
    """Why one detection window did or did not raise a signal.

    The first two mean the window could not be judged yet; the rest are
    the detector's checks in the order it applies them.
    """

    TOO_FEW_SAMPLES = "too_few_samples"
    SHORT_SPAN = "short_span"
    NO_UPWARD_TREND = "no_upward_trend"
    NOT_MONOTONIC = "not_monotonic"
    TOO_MANY_DROPS = "too_many_drops"
    BELOW_THRESHOLDS = "below_thresholds"
    QUALIFIED = "qualified"


# Verdicts for windows that held too little data to be judged.
UNJUDGED_VERDICTS = frozenset({WindowVerdict.TOO_FEW_SAMPLES, WindowVerdict.SHORT_SPAN})


@dataclass(frozen=True)
class WindowEvaluation:
    """The values one detection window was judged on, as the detector saw them.

    Emitted so views can explain a signal (or its absence) without
    re-running detection. Values are ``None`` for windows that held too
    few points to compute them. ``score`` is the heuristic severity score;
    it ranks growth against the thresholds and is not a probability.
    """

    window_seconds: float
    verdict: WindowVerdict
    points: int
    span_seconds: float
    baseline_value: float | None = None
    current_value: float | None = None
    slope_per_second: float | None = None
    monotonic_ratio: float | None = None
    drop_ratio: float | None = None
    score: float | None = None
    severity: LeakSeverity = LeakSeverity.NONE

    @property
    def judged(self) -> bool:
        """Whether the window held enough data for the growth checks to run."""
        return self.verdict not in UNJUDGED_VERDICTS

    @property
    def qualified(self) -> bool:
        return self.verdict == WindowVerdict.QUALIFIED

    @property
    def delta(self) -> float | None:
        if self.baseline_value is None or self.current_value is None:
            return None
        return self.current_value - self.baseline_value


@dataclass(frozen=True)
class DetectionCriteria:
    """The fixed rules a window must pass, as the detector applies them.

    ``severity_scores`` lists the minimum heuristic score for each tier,
    strongest first.
    """

    min_points: int
    min_span_ratio: float
    min_monotonic_ratio: float
    max_drop_ratio: float
    severity_scores: tuple[tuple[LeakSeverity, float], ...]


@dataclass(frozen=True)
class ResourceEvidence:
    """The detector's evidence for one resource at one sample.

    ``valid_samples`` and ``elapsed_seconds`` describe the current series
    segment: valid observations since the measured process set last
    changed. ``needed_samples`` and ``needed_seconds`` are what the shortest
    window requires before it can be judged. ``evaluated_window_seconds``
    is the longest window judged at this sample (the alerting one for
    ``GROWTH``), else ``None``. ``windows`` holds one ``WindowEvaluation``
    per configured window, shortest first (empty during warm-up and while
    nothing can be measured).
    """

    resource: LeakResource
    state: EvidenceState
    valid_samples: int = 0
    needed_samples: int = 0
    elapsed_seconds: float = 0.0
    needed_seconds: float = 0.0
    evaluated_window_seconds: float | None = None
    severity: LeakSeverity = LeakSeverity.NONE
    coverage: MetricCoverage | None = None
    windows: tuple[WindowEvaluation, ...] = ()

    @property
    def partial(self) -> bool:
        return self.coverage is not None and self.coverage.partial

    @property
    def unavailable_reason(self) -> str:
        """Why an ``UNAVAILABLE`` resource has no measurement.

        Once the monitored tree has exited there is nothing left to read,
        which is not the same as live processes refusing access; saying
        "no readable process" then would suggest a permissions problem.
        """
        if self.coverage is not None and self.coverage.total == 0:
            return "no live process"
        return "no readable process"


def unavailable_reason(evidence: Iterable[ResourceEvidence]) -> str:
    """The shared reason when every resource is unavailable at one sample."""
    reasons = {item.unavailable_reason for item in evidence}
    return reasons.pop() if len(reasons) == 1 else "no readable process"


# Per-process counters kept in bounded histories for growth attribution.
TRACKED_METRICS = ("private_bytes", "handles", "threads")


class LifecycleKind(str, Enum):
    """What happened to a process between two samples."""

    STARTED = "started"
    EXITED = "exited"
    # A start that replaces a process of the same name and parent which
    # exited moments before (a crashed or recycled worker coming back).
    RESTARTED = "restarted"


@dataclass(frozen=True)
class LifecycleEvent:
    """A process joining or leaving the monitored tree.

    ``replaces_pid`` is the exited process a ``RESTARTED`` start replaced.
    """

    kind: LifecycleKind
    timestamp: float
    pid: int
    name: str
    create_time: float | None = None
    ppid: int | None = None
    replaces_pid: int | None = None


class TimelineKind(str, Enum):
    """Something that happened during a session worth marking on a chart."""

    WARMUP_END = "warmup_end"
    STARTED = "started"
    EXITED = "exited"
    RESTARTED = "restarted"
    PAUSED = "paused"
    RESUMED = "resumed"
    SIGNAL = "signal"
    CLEARED = "cleared"
    ACCESS_LOST = "access_lost"
    ACCESS_REGAINED = "access_regained"
    MARKER = "marker"


@dataclass(frozen=True)
class TimelineMark:
    """One annotation on the session timeline.

    ``resource`` and ``severity`` are set for signal changes, ``pid`` for
    process and access changes, and ``index`` for user markers. A marker
    records when the user said something happened; it shows timing, not
    that the event caused any growth.
    """

    timestamp: float
    kind: TimelineKind
    label: str
    resource: LeakResource | None = None
    severity: LeakSeverity | None = None
    pid: int | None = None
    index: int | None = None


@dataclass(frozen=True)
class ProcessCounts:
    """Live processes now, and lifecycle totals since monitoring began.

    Restarts are also counted as starts, so ``started`` is every arrival.
    """

    live: int
    started: int = 0
    exited: int = 0
    restarted: int = 0


@dataclass(frozen=True)
class ProcessSummary:
    """What one process (one identity) did while it was monitored.

    ``first``, ``last`` and ``peak`` map ``TRACKED_METRICS`` to values; a
    metric that was never measured for the process is absent. ``exited_at``
    is set once the process has left the tree.
    """

    pid: int
    create_time: float | None
    name: str
    ppid: int | None
    first_seen: float
    last_seen: float
    samples: int
    first: dict[str, float]
    last: dict[str, float]
    peak: dict[str, float]
    exited_at: float | None = None

    @property
    def identity(self) -> tuple[int, float | None]:
        return (self.pid, self.create_time)

    @property
    def exited(self) -> bool:
        return self.exited_at is not None

    def growth(self, metric: str) -> float | None:
        """Change from the first to the last measured value, if both exist."""
        if metric not in self.first or metric not in self.last:
            return None
        return self.last[metric] - self.first[metric]


@dataclass(frozen=True)
class ProcessGrowth:
    """How one process's metric changed over an interval of its own history.

    ``delta`` is the last measured value in the interval minus the first,
    signed; ``slope_per_second`` the least-squares rate over those points.
    ``new_process`` marks one first seen inside the interval, ``exited``
    one that left the tree. This describes a process; it is not a signal.
    """

    pid: int
    create_time: float | None
    name: str
    ppid: int | None
    metric: str
    first_value: float
    last_value: float
    delta: float
    slope_per_second: float
    samples: int
    span_seconds: float
    new_process: bool = False
    exited: bool = False

    @property
    def identity(self) -> tuple[int, float | None]:
        return (self.pid, self.create_time)


@dataclass(frozen=True)
class ProcessMetrics:
    """Aggregated metrics for one process in the monitored tree.

    ``handle_types`` maps handle categories (see ``handle_types.CATEGORIES``)
    to counts on samples where handle types were classified, else ``None``.
    ``unmeasured`` names the counters (``METRIC_KEYS``) that could not be
    read; their fields hold 0 for compatibility but are unknown, not zero.
    ``create_time`` is the process creation time when known: with ``pid``
    it is the process ``identity``, so a reused PID is a different process.
    """

    pid: int
    name: str
    status: str
    rss_bytes: int
    vms_bytes: int
    private_bytes: int
    handles: int
    threads: int
    cpu_percent: float
    io_read_bytes_per_sec: float
    io_write_bytes_per_sec: float
    access_denied: bool = False
    ppid: int | None = None
    handle_types: dict[str, int] | None = None
    unmeasured: tuple[str, ...] = ()
    create_time: float | None = None

    @property
    def identity(self) -> tuple[int, float | None]:
        """``(pid, create_time)``: unique per process, unlike the PID alone."""
        return (self.pid, self.create_time)

    def measured(self, metric: str) -> bool:
        """Whether ``metric`` was read for this process (a zero is then real)."""
        return metric not in self.unmeasured


@dataclass(frozen=True)
class HostPressure:
    """Host context, never attributed to the monitored process tree.

    CPU is percent of total host capacity; memory values are physical bytes.
    Unknown readings remain ``None``. Age uses a monotonic clock at publication.
    """

    sampled_at: float
    age_seconds: float = 0.0
    cpu_percent: float | None = None
    available_memory_bytes: int | None = None
    total_memory_bytes: int | None = None
    collection_ms: float | None = None


@dataclass(frozen=True)
class MonitorDiagnostics:
    """How well and how cheaply the monitor itself sampled, at one snapshot.

    Measured by the detector on its own thread so a reader can judge
    whether a recording is trustworthy: the cadence actually achieved
    against the one requested, what this sample cost, how stale the
    handle-type breakdown is, how many samples failed or were dropped
    before a front end read them, and the monitor process's own CPU and
    memory. ``None`` marks a value that could not be measured (yet).
    """

    requested_interval_ms: int
    # Mean start-to-start interval over the recent collected samples.
    actual_interval_ms: float | None = None
    # Wall time spent collecting this sample (None on paused/failed ones).
    collection_ms: float | None = None
    # Seconds since handle types were last classified, and that pass's cost.
    handle_types_age_seconds: float | None = None
    handle_types_cost_ms: float | None = None
    failed_samples: int = 0
    dropped_snapshots: int = 0
    # The DrainTracer process: CPU % of one core since the previous sample.
    monitor_cpu_percent: float | None = None
    monitor_rss_bytes: int | None = None
    monitor_private_bytes: int | None = None
    host: HostPressure | None = None
    clock_adjustments: int = 0
    sampling_gaps: int = 0

    @property
    def cadence_lag_ms(self) -> float | None:
        """How much slower than requested sampling ran (negative: faster)."""
        if self.actual_interval_ms is None:
            return None
        return self.actual_interval_ms - self.requested_interval_ms


@dataclass(frozen=True)
class MetricsSnapshot:
    """Point-in-time view of the monitored process tree and leak state.

    ``handle_types`` holds tree-wide handle counts per category on samples
    where handle types were classified (every few seconds), else ``None``.
    ``warming_up`` is true while leak heuristics are deferred by a warm-up.
    ``coverage`` maps ``COVERAGE_METRICS`` to how many live processes each
    was read from (``None`` on snapshots that measured nothing new), and
    ``evidence`` holds one ``ResourceEvidence`` per signal resource.
    ``diagnostics`` describes the monitor's own sampling (``None`` on
    snapshots not built by the detector).
    """

    timestamp: float
    root_pid: int | None
    process_alive: bool
    monitoring_paused: bool
    processes: tuple[ProcessMetrics, ...]
    totals: dict[str, float]
    alerts: tuple[LeakAlert, ...]
    severity: LeakSeverity
    sample_interval_ms: int
    metadata: dict[str, Any] = field(default_factory=dict)
    handle_types: dict[str, int] | None = None
    warming_up: bool = False
    coverage: dict[str, MetricCoverage] | None = None
    evidence: tuple[ResourceEvidence, ...] = ()
    lifecycle: tuple[LifecycleEvent, ...] = ()
    process_counts: ProcessCounts | None = None
    process_summaries: tuple[ProcessSummary, ...] = ()
    exited_processes: tuple[ProcessSummary, ...] = ()
    diagnostics: MonitorDiagnostics | None = None

    def evidence_for(self, resource: LeakResource) -> ResourceEvidence | None:
        return next((item for item in self.evidence if item.resource == resource), None)

    @property
    def unmeasured_pids(self) -> tuple[int, ...]:
        """Live processes missing at least one covered metric, sorted."""
        if not self.coverage:
            return ()
        return tuple(
            sorted({pid for item in self.coverage.values() for pid in item.unmeasured_pids})
        )


SnapshotCallback = Callable[[MetricsSnapshot], None]


class MetricsStream:
    """Thread-safe queue and callback bridge for metrics snapshots."""

    def __init__(self, max_queue_size: int = 256, *, facts: SessionFacts | None = None) -> None:
        self._queue: Queue[MetricsSnapshot] = Queue(maxsize=max_queue_size)
        self._callbacks: list[SnapshotCallback] = []
        self._lock = threading.Lock()
        self._dropped = 0
        self.facts = facts

    @property
    def dropped(self) -> int:
        """Snapshots discarded because the queue was full when they arrived."""
        with self._lock:
            return self._dropped

    def subscribe(self, callback: SnapshotCallback) -> None:
        """Register a callback invoked on every published snapshot."""
        with self._lock:
            if callback not in self._callbacks:
                self._callbacks.append(callback)

    def unsubscribe(self, callback: SnapshotCallback) -> None:
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

    def publish(self, snapshot: MetricsSnapshot) -> None:
        """Push a snapshot to subscribers and the internal queue."""
        with self._lock:
            callbacks = list(self._callbacks)
            if self._queue.full():
                try:
                    self._queue.get_nowait()
                    self._dropped += 1
                except Empty:
                    pass  # A consumer drained it after the fullness check.
            if snapshot.diagnostics is not None:
                snapshot = replace(snapshot, diagnostics=replace(
                    snapshot.diagnostics,
                    dropped_snapshots=max(snapshot.diagnostics.dropped_snapshots, self._dropped),
                ))
            # Fixed-size accounting precedes visibility to the consumer and
            # survives queue eviction. No UI work runs on this path.
            if self.facts is not None:
                self.facts.observe(snapshot, dropped=self._dropped)
            self._queue.put_nowait(snapshot)

        for callback in callbacks:
            try:
                callback(snapshot)
            except Exception:
                # Subscriber failures must not break the sampling loop.
                pass

    def get_latest(self, timeout: float | None = None) -> MetricsSnapshot | None:
        """Return the next queued snapshot, or None on timeout."""
        try:
            return self._queue.get(timeout=timeout)
        except Empty:
            return None

    def drain(self) -> list[MetricsSnapshot]:
        """Return and remove all queued snapshots."""
        items: list[MetricsSnapshot] = []
        while True:
            try:
                items.append(self._queue.get_nowait())
            except Empty:
                break
        return items

    def clear(self) -> None:
        """Discard all queued snapshots."""
        while True:
            try:
                self._queue.get_nowait()
            except Empty:
                break


def empty_snapshot(
    sample_interval_ms: int = 500,
    metadata: dict[str, Any] | None = None,
) -> MetricsSnapshot:
    """Build a neutral snapshot used before monitoring starts."""
    return MetricsSnapshot(
        timestamp=time.time(),
        root_pid=None,
        process_alive=False,
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
        sample_interval_ms=sample_interval_ms,
        metadata=metadata or {},
    )
