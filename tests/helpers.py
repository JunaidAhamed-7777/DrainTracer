"""Shared builders for DrainTracer tests."""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from rich.console import Console  # noqa: E402

from src.events import (  # noqa: E402
    METRIC_KEYS,
    LeakAlert,
    LeakResource,
    LeakSeverity,
    MetricsSnapshot,
    ProcessMetrics,
)


def make_process(
    pid: int,
    ppid: int | None = None,
    name: str = "worker.exe",
    rss: int = 10 * 1024 * 1024,
    handles: int = 50,
    threads: int = 4,
) -> ProcessMetrics:
    return ProcessMetrics(
        pid=pid,
        name=name,
        status="running",
        rss_bytes=rss,
        vms_bytes=rss * 2,
        private_bytes=rss,
        handles=handles,
        threads=threads,
        cpu_percent=1.5,
        io_read_bytes_per_sec=0.0,
        io_write_bytes_per_sec=0.0,
        ppid=ppid,
    )


def make_denied(pid: int, ppid: int | None = None) -> ProcessMetrics:
    """A process whose counters could not be read, as the sampler reports it."""
    return ProcessMetrics(
        pid=pid,
        name="access-denied",
        status="unknown",
        rss_bytes=0,
        vms_bytes=0,
        private_bytes=0,
        handles=0,
        threads=0,
        cpu_percent=0.0,
        io_read_bytes_per_sec=0.0,
        io_write_bytes_per_sec=0.0,
        access_denied=True,
        ppid=ppid,
        unmeasured=tuple(METRIC_KEYS),
    )


def coverage_of(processes):
    """The coverage the detector would record for ``processes``."""
    from src.leak_detector import LeakDetector

    return LeakDetector._coverage(processes)


def make_alert(
    resource: LeakResource = LeakResource.MEMORY,
    severity: LeakSeverity = LeakSeverity.HIGH,
) -> LeakAlert:
    return LeakAlert(
        resource=resource,
        severity=severity,
        message=f"Potential {resource.value} leak: +10 over 10s",
        slope_per_second=1.0,
        window_seconds=10.0,
        current_value=110.0,
        baseline_value=100.0,
    )


def make_snapshot(
    processes: tuple[ProcessMetrics, ...] = (),
    alerts: tuple[LeakAlert, ...] = (),
    root_pid: int | None = 100,
    timestamp: float | None = None,
    paused: bool = False,
    metadata: dict | None = None,
    measured_coverage: bool = False,
) -> MetricsSnapshot:
    """A snapshot of ``processes``; ``measured_coverage`` adds their coverage."""
    processes = processes or (make_process(root_pid or 100),)
    totals = {
        "rss_bytes": float(sum(p.rss_bytes for p in processes)),
        "vms_bytes": float(sum(p.vms_bytes for p in processes)),
        "private_bytes": float(sum(p.private_bytes for p in processes)),
        "handles": float(sum(p.handles for p in processes)),
        "threads": float(sum(p.threads for p in processes)),
        "cpu_percent": float(sum(p.cpu_percent for p in processes)),
        "io_read_bytes_per_sec": 0.0,
        "io_write_bytes_per_sec": 0.0,
    }
    severity = LeakSeverity.NONE
    for alert in alerts:
        severity = alert.severity
    return MetricsSnapshot(
        timestamp=time.time() if timestamp is None else timestamp,
        root_pid=root_pid,
        process_alive=True,
        monitoring_paused=paused,
        processes=processes,
        totals=totals,
        alerts=alerts,
        severity=severity,
        sample_interval_ms=500,
        metadata=metadata or {},
        coverage=coverage_of(processes) if measured_coverage else None,
    )


class EncodedBuffer(io.StringIO):
    """StringIO that reports a chosen console encoding."""

    def __init__(self, encoding: str = "utf-8") -> None:
        super().__init__()
        self._encoding = encoding

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return self._encoding


def make_console(width: int = 140, height: int = 40, encoding: str = "utf-8") -> Console:
    return Console(
        file=EncodedBuffer(encoding),
        width=width,
        height=height,
        color_system=None,
        force_terminal=False,
    )
