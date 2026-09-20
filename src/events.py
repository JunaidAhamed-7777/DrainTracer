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


class LeakResource(str, Enum):
    """Resource category flagged by leak heuristics."""

    MEMORY = "memory"
    HANDLES = "handles"
    THREADS = "threads"


@dataclass(frozen=True)
class LeakAlert:
    """A single leak heuristic result for one resource category."""

    resource: LeakResource
    severity: LeakSeverity
    message: str
    slope_per_second: float
    window_seconds: float
    current_value: float
    baseline_value: float


@dataclass(frozen=True)
class ProcessMetrics:
    """Aggregated metrics for one process in the monitored tree."""

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


@dataclass(frozen=True)
class MetricsSnapshot:
    """Point-in-time view of the monitored process tree and leak state."""

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
