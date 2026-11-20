"""Read-only discovery and sampling of individual running processes."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import psutil


@dataclass(frozen=True)
class RunningProcess:
    """A displayed process and its identity when the list was collected."""

    pid: int
    name: str
    create_time: float | None


@dataclass(frozen=True)
class ProcessReading:
    """One observation of a selected PID, pinned to its creation time."""

    process: RunningProcess
    error: str | None = None
    sampled_at: float = 0.0
    cpu_seconds: float | None = None
    cpu_percent: float | None = None
    resident_bytes: int | None = None
    threads: int | None = None
    handles: int | None = None


def sample_process(pid: int, previous: ProcessReading | None = None) -> ProcessReading:
    """Read one PID without following its children or rebinding an exited selection."""
    if previous is not None and previous.error:
        return previous
    identity = previous.process if previous else RunningProcess(pid, "Unknown program", None)
    handles_attr = "num_handles" if psutil.WINDOWS else "num_fds"
    try:
        process = psutil.Process(pid)
        created = process.create_time()
        if previous is not None and created != identity.create_time:
            return ProcessReading(identity, error="Exited (PID reused)")
        info = process.as_dict(["name", "memory_info", "num_threads", handles_attr, "cpu_times"])
        candidate = RunningProcess(pid, info["name"] or identity.name, created)
        if not is_process_current(candidate):
            return ProcessReading(identity, error="Exited or identity unavailable")
    except psutil.NoSuchProcess:
        return ProcessReading(identity, error="Process exited or PID does not exist")
    except psutil.AccessDenied:
        return ProcessReading(identity, error="Permission denied")
    except (OSError, OverflowError, ValueError, NotImplementedError) as exc:
        return ProcessReading(identity, error=str(exc))

    stamp = time.monotonic()
    times = info["cpu_times"]
    seconds = times.user + times.system if times is not None else None
    percent = None
    if (previous is not None and previous.cpu_seconds is not None and seconds is not None
            and stamp > previous.sampled_at and seconds >= previous.cpu_seconds):
        percent = 100.0 * (seconds - previous.cpu_seconds) / (stamp - previous.sampled_at)
    memory = info["memory_info"]
    return ProcessReading(
        candidate, sampled_at=stamp, cpu_seconds=seconds, cpu_percent=percent,
        resident_bytes=memory.rss if memory is not None else None,
        threads=info["num_threads"], handles=info[handles_attr],
    )


def list_running_processes() -> list[RunningProcess]:
    """List visible processes; let enumeration failures reach the caller."""
    # psutil 6+ caches identities without checking every PID for reuse.
    # Older supported releases handle reuse internally and have no reset API.
    clear_cache = getattr(psutil.process_iter, "cache_clear", None)
    if callable(clear_cache):
        clear_cache()
    own_pid = os.getpid()
    processes = []
    for process in psutil.process_iter(["pid", "name", "create_time", "status"], ad_value=None):
        try:
            info = process.info
            pid = info["pid"]
            if (pid <= 0 or pid == own_pid
                    or info.get("status") in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD)):
                continue
            processes.append(
                RunningProcess(pid, info.get("name") or "Unknown program", info.get("create_time"))
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return sorted(processes, key=lambda process: (process.name.casefold(), process.pid))


def is_process_current(process: RunningProcess) -> bool:
    """Reject unavailable or changed identities before accepting a selection.

    Creation times identify processes on both Windows and Linux. Attachment still
    belongs to the process manager, which performs its own lifecycle checks.
    """
    if process.create_time is None:
        return False
    try:
        current = psutil.Process(process.pid)
        return (current.create_time() == process.create_time and current.is_running()
                and current.status() not in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD))
    except (psutil.Error, OSError):
        return False
