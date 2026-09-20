"""Background resource sampling and leak detection heuristics."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
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
from .process_runner import ProcessManager

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_INTERVAL_MS = 500
DEFAULT_WINDOWS_SECONDS = (10.0, 30.0, 60.0)


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

    def values_in_window(self, timestamp: float, window_seconds: float) -> list[float]:
        cutoff = timestamp - window_seconds
        return [p.value for p in self.points if p.timestamp >= cutoff]

    def moving_average(self, timestamp: float, window_seconds: float) -> float | None:
        values = self.values_in_window(timestamp, window_seconds)
        if not values:
            return None
        return sum(values) / len(values)


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
    ) -> None:
        self.process_manager = process_manager
        self.stream = stream or MetricsStream()
        self.sample_interval_ms = sample_interval_ms
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
            metadata={"last_error": self.process_manager.last_error},
        )

    def _collect_snapshot(self) -> MetricsSnapshot:
        now = time.time()
        self.process_manager.refresh_process_tree()
        processes = self._sample_processes(now)
        totals = self._aggregate_totals(processes)

        self._memory_series.add(now, totals["private_bytes"], self.max_window_seconds * 1.5)
        self._handle_series.add(now, totals["handles"], self.max_window_seconds * 1.5)
        self._thread_series.add(now, totals["threads"], self.max_window_seconds * 1.5)

        alerts = self._evaluate_leaks(now, totals)
        severity = self._max_severity(alerts)

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
        )

    def _sample_processes(self, now: float) -> list[ProcessMetrics]:
        metrics: list[ProcessMetrics] = []
        for proc in self.process_manager.iter_psutil_processes():
            metrics.append(self._sample_process(proc, now))
        return metrics

    def _sample_process(self, proc: psutil.Process, now: float) -> ProcessMetrics:
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
                cpu_percent = self._sample_cpu_percent(proc, now)
                io_read_bps, io_write_bps = self._sample_io_rates(proc, now)
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
        )

    @staticmethod
    def _safe_num_handles(proc: psutil.Process) -> int:
        try:
            return int(proc.num_handles())
        except (AttributeError, OSError, psutil.AccessDenied, NotImplementedError):
            return 0

    def _sample_cpu_percent(self, proc: psutil.Process, now: float) -> float:
        pid = proc.pid
        try:
            cpu = proc.cpu_percent(interval=None)
        except (psutil.Error, OSError):
            return 0.0

        last = self._cpu_baselines.get(pid)
        if last is None:
            self._cpu_baselines[pid] = (now, cpu)
            return 0.0

        self._cpu_baselines[pid] = (now, cpu)
        return float(cpu)

    def _sample_io_rates(self, proc: psutil.Process, now: float) -> tuple[float, float]:
        pid = proc.pid
        try:
            counters = proc.io_counters()
            read_bytes = int(counters.read_bytes)
            write_bytes = int(counters.write_bytes)
        except (psutil.Error, OSError, AttributeError):
            return 0.0, 0.0

        baseline = self._io_baselines.get(pid)
        if baseline is None:
            self._io_baselines[pid] = (now, read_bytes, write_bytes)
            return 0.0, 0.0

        last_time, last_read, last_write = baseline
        elapsed = max(now - last_time, 1e-6)
        read_bps = max(0.0, (read_bytes - last_read) / elapsed)
        write_bps = max(0.0, (write_bytes - last_write) / elapsed)
        self._io_baselines[pid] = (now, read_bytes, write_bytes)
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

        for window in self.window_seconds:
            values = series.values_in_window(timestamp, window)
            if len(values) < max(4, int(window / (self.sample_interval_ms / 1000.0))):
                continue

            slope = self._least_squares_slope(values, self.sample_interval_ms / 1000.0)
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
                f"+{delta:.0f} over {window:.0f}s "
                f"(slope {slope:.2f}/s, monotonic {monotonic_ratio:.0%})"
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

        return best_alert

    @staticmethod
    def _least_squares_slope(values: list[float], sample_interval: float) -> float:
        n = len(values)
        if n < 2:
            return 0.0
        xs = [i * sample_interval for i in range(n)]
        x_mean = sum(xs) / n
        y_mean = sum(values) / n
        numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, values))
        denominator = sum((x - x_mean) ** 2 for x in xs)
        if denominator == 0:
            return 0.0
        return numerator / denominator

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
