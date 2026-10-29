"""Read-only process discovery for selecting an existing monitoring target."""

from __future__ import annotations

import os
from dataclasses import dataclass

import psutil


@dataclass(frozen=True)
class RunningProcess:
    """A displayed process and its identity when the list was collected."""

    pid: int
    name: str
    create_time: float | None


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
