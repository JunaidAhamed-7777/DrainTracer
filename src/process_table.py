"""Thread counts and suspension for many processes from one system query.

psutil answers ``num_threads()`` and ``status()`` on Windows by enumerating
every process on the system, once per call, so sampling a tree cost two
system-wide enumerations per process. One
``NtQuerySystemInformation(SystemProcessInformation)`` pass per sample
answers both for every tracked process at once. Callers fall back to psutil
whenever this table is unavailable.
"""

from __future__ import annotations

import ctypes
import logging
import struct
import sys
from collections.abc import Collection
from typing import NamedTuple

logger = logging.getLogger(__name__)

_SYSTEM_PROCESS_INFORMATION = 5
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
_MAX_BUFFER = 256 * 1024 * 1024
# A thread in KTHREAD_STATE Waiting (5) for KWAIT_REASON Suspended (5).
_SUSPENDED_THREAD = (5, 5)
_FAILURES_BEFORE_DISABLING = 3


class ProcessEntry(NamedTuple):
    """What the system process table says about one process."""

    threads: int
    suspended: bool


class ProcessTable:
    """Read thread counts and suspended states from one system snapshot."""

    def __init__(self) -> None:
        self._enabled = sys.platform == "win32"
        self._query = None
        self._buffer_size = 1 << 20
        self._failures = 0

    @property
    def available(self) -> bool:
        return self._enabled

    def snapshot(self, pids: Collection[int]) -> dict[int, ProcessEntry] | None:
        """Entries for those ``pids`` that are running, or ``None`` if unavailable."""
        if not self._enabled:
            return None
        try:
            entries = parse_process_table(self._read(), set(pids))
        except (OSError, ValueError, struct.error) as exc:
            self._failures += 1
            logger.debug("System process table unavailable: %s", exc)
            if self._failures >= _FAILURES_BEFORE_DISABLING:
                self._enabled = False
            return None
        self._failures = 0
        return entries

    def _read(self) -> bytes:
        from ctypes import wintypes

        if self._query is None:
            query = ctypes.WinDLL("ntdll").NtQuerySystemInformation
            query.argtypes = [
                wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)
            ]
            query.restype = wintypes.LONG
            self._query = query
        size = self._buffer_size
        while True:
            buffer = ctypes.create_string_buffer(size)
            needed = wintypes.ULONG(0)
            status = self._query(
                _SYSTEM_PROCESS_INFORMATION, buffer, size, ctypes.byref(needed)
            ) & 0xFFFFFFFF
            if status == _STATUS_INFO_LENGTH_MISMATCH:
                # Processes come and go between calls; leave headroom.
                size = max(size * 2, needed.value + (1 << 16))
                if size > _MAX_BUFFER:
                    raise OSError("the system process table is too large to read")
                continue
            if status != 0:
                raise OSError(f"NtQuerySystemInformation failed with status 0x{status:08X}")
            self._buffer_size = size
            return buffer.raw


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("MaximumLength", ctypes.c_ushort),
        ("Buffer", ctypes.c_void_p),
    ]


class _ClientId(ctypes.Structure):
    _fields_ = [("UniqueProcess", ctypes.c_void_p), ("UniqueThread", ctypes.c_void_p)]


class _SystemThreadInformation(ctypes.Structure):
    _fields_ = [
        ("KernelTime", ctypes.c_longlong),
        ("UserTime", ctypes.c_longlong),
        ("CreateTime", ctypes.c_longlong),
        ("WaitTime", ctypes.c_ulong),
        ("StartAddress", ctypes.c_void_p),
        ("ClientId", _ClientId),
        ("Priority", ctypes.c_long),
        ("BasePriority", ctypes.c_long),
        ("ContextSwitches", ctypes.c_ulong),
        ("ThreadState", ctypes.c_ulong),
        ("WaitReason", ctypes.c_ulong),
    ]


class _SystemProcessInformation(ctypes.Structure):
    _fields_ = [
        ("NextEntryOffset", ctypes.c_ulong),
        ("NumberOfThreads", ctypes.c_ulong),
        ("WorkingSetPrivateSize", ctypes.c_longlong),
        ("HardFaultCount", ctypes.c_ulong),
        ("NumberOfThreadsHighWatermark", ctypes.c_ulong),
        ("CycleTime", ctypes.c_ulonglong),
        ("CreateTime", ctypes.c_longlong),
        ("UserTime", ctypes.c_longlong),
        ("KernelTime", ctypes.c_longlong),
        ("ImageName", _UnicodeString),
        ("BasePriority", ctypes.c_long),
        ("UniqueProcessId", ctypes.c_void_p),
        ("InheritedFromUniqueProcessId", ctypes.c_void_p),
        ("HandleCount", ctypes.c_ulong),
        ("SessionId", ctypes.c_ulong),
        ("UniqueProcessKey", ctypes.c_void_p),
        ("PeakVirtualSize", ctypes.c_size_t),
        ("VirtualSize", ctypes.c_size_t),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivatePageCount", ctypes.c_size_t),
        ("ReadOperationCount", ctypes.c_longlong),
        ("WriteOperationCount", ctypes.c_longlong),
        ("OtherOperationCount", ctypes.c_longlong),
        ("ReadTransferCount", ctypes.c_longlong),
        ("WriteTransferCount", ctypes.c_longlong),
        ("OtherTransferCount", ctypes.c_longlong),
    ]


_HEADER = ctypes.sizeof(_SystemProcessInformation)
_THREAD = ctypes.sizeof(_SystemThreadInformation)
_PID_AT = _SystemProcessInformation.UniqueProcessId.offset
_PID_FORMAT = "<Q" if ctypes.sizeof(ctypes.c_void_p) == 8 else "<I"
_STATE_AT = _SystemThreadInformation.ThreadState.offset
assert _SystemThreadInformation.WaitReason.offset == _STATE_AT + 4


def parse_process_table(raw: bytes, wanted: set[int]) -> dict[int, ProcessEntry]:
    """Walk ``SYSTEM_PROCESS_INFORMATION`` records, decoding only ``wanted`` PIDs.

    Every record and thread list is bounds-checked; a malformed table raises
    ``ValueError`` rather than reading past the buffer.
    """
    entries: dict[int, ProcessEntry] = {}
    offset = 0
    while True:
        if offset + _HEADER > len(raw):
            raise ValueError("truncated process record")
        next_offset, count = struct.unpack_from("<II", raw, offset)
        (pid,) = struct.unpack_from(_PID_FORMAT, raw, offset + _PID_AT)
        if pid in wanted:
            first = offset + _HEADER
            if first + count * _THREAD > len(raw):
                raise ValueError("truncated thread list")
            suspended = all(
                struct.unpack_from("<II", raw, first + index * _THREAD + _STATE_AT)
                == _SUSPENDED_THREAD
                for index in range(count)
            )
            entries[pid] = ProcessEntry(threads=count, suspended=suspended)
        if next_offset == 0:
            return entries
        if next_offset < _HEADER:
            raise ValueError("process records overlap")
        offset += next_offset


__all__ = ["ProcessEntry", "ProcessTable", "parse_process_table"]
