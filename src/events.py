"""Event stream and structured data models for the leak detector UI layer."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from queue import Empty, Queue
from typing import Any


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
class LeakAlert:
    """A single leak heuristic result for one resource category.

    ``growth_by_type`` is set for handle alerts when handle types were
    classified during the window: the change per handle category between
    the first classification in the window and the latest one.
    """

    resource: LeakResource
    severity: LeakSeverity
    message: str
    slope_per_second: float
    window_seconds: float
    current_value: float
    baseline_value: float
    growth_by_type: dict[str, int] | None = None

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


@dataclass(frozen=True)
class ResourceEvidence:
    """The detector's evidence for one resource at one sample.

    ``valid_samples`` and ``elapsed_seconds`` describe the current series
    segment: valid observations since the measured process set last
    changed. ``needed_samples`` and ``needed_seconds`` are what the shortest
    window requires before it can be judged. ``evaluated_window_seconds``
    is the longest window judged at this sample (the alerting one for
    ``GROWTH``), else ``None``.
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

    @property
    def partial(self) -> bool:
        return self.coverage is not None and self.coverage.partial


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
class MetricsSnapshot:
    """Point-in-time view of the monitored process tree and leak state.

    ``handle_types`` holds tree-wide handle counts per category on samples
    where handle types were classified (every few seconds), else ``None``.
    ``warming_up`` is true while leak heuristics are deferred by a warm-up.
    ``coverage`` maps ``COVERAGE_METRICS`` to how many live processes each
    was read from (``None`` on snapshots that measured nothing new), and
    ``evidence`` holds one ``ResourceEvidence`` per signal resource.
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

    def __init__(self, max_queue_size: int = 256) -> None:
        self._queue: Queue[MetricsSnapshot] = Queue(maxsize=max_queue_size)
        self._callbacks: list[SnapshotCallback] = []
        self._lock = threading.Lock()

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

        for callback in callbacks:
            try:
                callback(snapshot)
            except Exception:
                # UI callbacks must not break the sampling loop.
                pass

        try:
            self._queue.put_nowait(snapshot)
        except Exception:
            try:
                self._queue.get_nowait()
            except Empty:
                pass
            try:
                self._queue.put_nowait(snapshot)
            except Exception:
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
