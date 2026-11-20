"""Read host resources and rank visible processes for the startup overview."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from heapq import nlargest
from typing import TypeVar

import psutil

from .process_table import ProcessTable
from .running_processes import RunningProcess

DEFAULT_PROCESS_LIMIT = 5
_Metric = TypeVar("_Metric")


@dataclass(frozen=True)
class RankedProcess:
    process: RunningProcess
    value: int | float


@dataclass(frozen=True)
class TransferRates:
    """Bytes per second in each direction. Empty rates are still warming up."""

    incoming: float | None = None
    outgoing: float | None = None


class _TransferMeter:
    def __init__(self) -> None:
        self.previous: tuple[float, int, int] | None = None

    def update(self, stamp: float, counts: tuple[int, int] | None) -> TransferRates | None:
        previous = self.previous
        if counts is None:
            self.previous = None
            return None
        incoming, outgoing = counts
        self.previous = (stamp, incoming, outgoing)
        if previous is None:
            return TransferRates()
        previous_stamp, previous_in, previous_out = previous
        elapsed = stamp - previous_stamp
        if elapsed <= 0 or incoming < previous_in or outgoing < previous_out:
            return TransferRates()
        return TransferRates((incoming - previous_in) / elapsed, (outgoing - previous_out) / elapsed)


@dataclass(frozen=True)
class SystemOverview:
    cpu_percent: float | None
    used_memory_bytes: int | None
    total_memory_bytes: int | None
    top_cpu: tuple[RankedProcess, ...]
    top_memory: tuple[RankedProcess, ...]
    top_handles: tuple[RankedProcess, ...]
    top_threads: tuple[RankedProcess, ...]
    handle_label: str
    sampled_at: float | None = None
    used_swap_bytes: int | None = None
    total_swap_bytes: int | None = None
    disk: TransferRates | None = None
    network: TransferRates | None = None
    cpu_warming_up: bool = False


def _read_metric(read: Callable[[], _Metric]) -> _Metric | None:
    """Allow unreadable metrics while leaving process exits to the caller."""
    try:
        return read()
    except (psutil.AccessDenied, OSError, NotImplementedError):
        return None


class SystemOverviewSampler:
    """Sample without a timed wait. Call from one worker thread to refresh.

    The first sample establishes CPU baselines. Host CPU is machine-wide and
    process CPU measures percentage of one core over the last sample interval.
    """

    def __init__(self, limit: int = DEFAULT_PROCESS_LIMIT) -> None:
        self._limit = limit
        self._host_cpu_primed = False
        self._disk = _TransferMeter()
        self._network = _TransferMeter()
        self._cpu_baselines: dict[tuple[int, float], tuple[float, float]] = {}
        self._process_table = ProcessTable()

    def sample(self, *, should_stop: Callable[[], bool] | None = None) -> SystemOverview:
        """Read one snapshot, checking for cancellation between processes."""
        host_cpu = _read_metric(lambda: psutil.cpu_percent(interval=None))
        cpu_percent = host_cpu if self._host_cpu_primed else None
        cpu_warming_up = host_cpu is not None and not self._host_cpu_primed
        self._host_cpu_primed = host_cpu is not None
        memory = _read_metric(psutil.virtual_memory)
        used = memory.total - memory.available if memory is not None else None
        total = memory.total if memory is not None else None
        swap = _read_metric(psutil.swap_memory)
        stamp = time.monotonic()
        disk = _read_metric(psutil.disk_io_counters)
        network = _read_metric(psutil.net_io_counters)
        disk_rates = self._disk.update(stamp, (disk.read_bytes, disk.write_bytes) if disk else None)
        network_rates = self._network.update(stamp, (network.bytes_recv, network.bytes_sent) if network else None)

        cpu_rows: list[RankedProcess] = []
        memory_rows: list[RankedProcess] = []
        handle_rows: list[RankedProcess] = []
        thread_rows: list[RankedProcess] = []
        next_baselines: dict[tuple[int, float], tuple[float, float]] = {}
        windows = psutil.WINDOWS
        handle_method = "num_handles" if windows else "num_fds"
        own_pid = os.getpid()
        pids = _read_metric(psutil.pids) or []
        # On Windows, num_threads() queries the whole system for each PID.
        # Read the table once and fall back when an entry is unavailable.
        thread_entries = (self._process_table.snapshot(pids) or {}) if windows and pids else {}
        # Fresh process objects give current creation times when an OS reuses a
        # PID and do not affect the independent process picker's iterator cache.
        for pid in pids:
            if should_stop is not None and should_stop():
                break
            if pid <= 0 or pid == own_pid:
                continue
            try:
                process = psutil.Process(pid)
                with process.oneshot():
                    # Windows status only distinguishes running and suspended
                    # and requires an expensive system-wide thread query.
                    if not windows:
                        status = _read_metric(process.status)
                        if status in (psutil.STATUS_DEAD, psutil.STATUS_ZOMBIE):
                            continue
                    created = _read_metric(process.create_time)
                    name = _read_metric(process.name) or "Unknown program"
                    times = _read_metric(process.cpu_times)
                    stamp = time.monotonic()
                    resident = _read_metric(process.memory_info)
                    count_handles = getattr(process, handle_method, None)
                    handles = _read_metric(count_handles) if count_handles is not None else None
                    entry = thread_entries.get(pid)
                    threads = entry.threads if entry is not None else _read_metric(process.num_threads)
            except (psutil.Error, OSError):
                continue

            row = RunningProcess(pid, name, created)
            if times is not None and created is not None:
                key = (pid, created)
                used_cpu = times.user + times.system
                next_baselines[key] = (stamp, used_cpu)
                previous = self._cpu_baselines.get(key)
                if previous is not None:
                    previous_stamp, previous_cpu = previous
                    elapsed = stamp - previous_stamp
                    if elapsed > 0 and used_cpu >= previous_cpu:
                        cpu_rows.append(RankedProcess(row, 100.0 * (used_cpu - previous_cpu) / elapsed))
            if resident is not None:
                memory_rows.append(RankedProcess(row, resident.rss))
            if handles is not None:
                handle_rows.append(RankedProcess(row, handles))
            if threads is not None:
                thread_rows.append(RankedProcess(row, threads))

        self._cpu_baselines = next_baselines
        return SystemOverview(
            cpu_percent, used, total,
            self._rank(cpu_rows), self._rank(memory_rows),
            self._rank(handle_rows), self._rank(thread_rows),
            "Handles" if windows else "Open FDs", time.time(),
            used_swap_bytes=swap.used if swap is not None else None,
            total_swap_bytes=swap.total if swap is not None else None,
            disk=disk_rates, network=network_rates, cpu_warming_up=cpu_warming_up,
        )

    def _rank(self, processes: list[RankedProcess]) -> tuple[RankedProcess, ...]:
        return tuple(nlargest(self._limit, processes, key=lambda row: (row.value, -row.process.pid)))
