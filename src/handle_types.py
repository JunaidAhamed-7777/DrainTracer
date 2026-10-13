"""Classify the Windows handles of monitored processes by object type.

One pass reads the system handle table with
``NtQuerySystemInformation(SystemExtendedHandleInformation)`` and maps each
entry's object-type index to a name from ``NtQueryObject(ObjectTypesInformation)``.
No handle is ever duplicated into DrainTracer, so classification needs no
access to the monitored processes and cannot hang on pipe handles.

Sockets are ``File`` objects on Windows (``\\Device\\Afd``), so they are
counted from the TCP/UDP connection table instead and moved from the file
count to the socket count.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from collections import Counter
from collections.abc import Collection, Mapping

import psutil

logger = logging.getLogger(__name__)

# Categories in display order; every handle lands in exactly one of them.
CATEGORIES = (
    "file",
    "socket",
    "registry",
    "event",
    "thread",
    "process",
    "mutex",
    "semaphore",
    "section",
    "other",
)

# Kernel object-type names that map to a named category.
_TYPE_CATEGORIES = {
    "File": "file",
    "Key": "registry",
    "Event": "event",
    "Thread": "thread",
    "Process": "process",
    "Mutant": "mutex",
    "Semaphore": "semaphore",
    "Section": "section",
}

_SYSTEM_EXTENDED_HANDLE_INFORMATION = 64
_OBJECT_TYPES_INFORMATION = 3
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
_MAX_BUFFER = 512 * 1024 * 1024
_FAILURES_BEFORE_DISABLING = 3

HandleCounts = dict[str, int]


class HandleClassifier:
    """Count handles per category for a set of PIDs.

    ``classify`` returns ``None`` when classification is unavailable (not
    Windows, or the system query keeps failing); callers then fall back to
    total handle counts.
    """

    def __init__(self) -> None:
        self._enabled = sys.platform == "win32"
        self._api: _NtApi | None = None
        self._type_names: dict[int, str] = {}
        self._buffer_size = 1 << 20
        self._failures = 0
        self.last_error: str | None = None

    @property
    def available(self) -> bool:
        return self._enabled

    def classify(self, pids: Collection[int]) -> dict[int, HandleCounts] | None:
        """Return ``{pid: {category: count}}`` for every PID in ``pids``."""
        if not self._enabled:
            return None
        wanted = set(pids)
        if not wanted:
            return {}
        try:
            api = self._load_api()
            by_type = self._count_by_type(api, wanted)
            if any(index not in self._type_names for counts in by_type.values() for index in counts):
                # Types are registered at boot; refresh if a new one appears.
                self._type_names = api.type_names()
        except (OSError, ValueError, AttributeError) as exc:
            self._failures += 1
            self.last_error = str(exc)
            logger.debug("Handle classification failed: %s", exc)
            if self._failures >= _FAILURES_BEFORE_DISABLING:
                self._enabled = False
            return None
        self._failures = 0
        sockets = _socket_counts(wanted)

        result: dict[int, HandleCounts] = {}
        for pid in wanted:
            counts: Counter[str] = Counter()
            for index, count in by_type.get(pid, {}).items():
                name = self._type_names.get(index, "")
                counts[_TYPE_CATEGORIES.get(name, "other")] += count
            moved = min(sockets.get(pid, 0), counts["file"])
            counts["file"] -= moved
            counts["socket"] += moved
            result[pid] = {category: counts[category] for category in CATEGORIES}
        return result

    def _load_api(self) -> _NtApi:
        if self._api is None:
            self._api = _NtApi()
            self._type_names = self._api.type_names()
        return self._api

    def _count_by_type(self, api: _NtApi, pids: set[int]) -> dict[int, Counter[int]]:
        table, self._buffer_size = api.handle_table(self._buffer_size)
        counts: dict[int, Counter[int]] = {}
        for pid, type_index in zip(*_handle_columns(table)):
            if pid in pids:
                counts.setdefault(pid, Counter())[type_index] += 1
        return counts


def describe_type_growth(growth: Mapping[str, int], limit: int = 2) -> str:
    """Name the categories that grew most, e.g. ``by type: file +390, event +8``.

    Returns an empty string when no category grew.
    """
    rising = sorted(
        ((count, category) for category, count in growth.items() if count > 0),
        key=lambda item: (-item[0], CATEGORIES.index(item[1]) if item[1] in CATEGORIES else 99),
    )[:limit]
    if not rising:
        return ""
    return "by type: " + ", ".join(f"{category} +{count:,}" for count, category in rising)


def total_counts(per_pid: Mapping[int, Mapping[str, int]]) -> HandleCounts:
    """Sum per-process category counts into one tree total."""
    totals = dict.fromkeys(CATEGORIES, 0)
    for counts in per_pid.values():
        for category in CATEGORIES:
            totals[category] += int(counts.get(category, 0))
    return totals


def _socket_counts(pids: set[int]) -> dict[int, int]:
    """Open TCP/UDP sockets per PID from the system connection table."""
    try:
        connections = psutil.net_connections(kind="inet")
    except (psutil.Error, OSError):
        return {}
    return dict(Counter(conn.pid for conn in connections if conn.pid in pids))


def _handle_columns(table: memoryview) -> tuple[memoryview, memoryview]:
    """Return parallel views of each entry's owning PID and object-type index.

    The table is ``SYSTEM_HANDLE_INFORMATION_EX``: a pointer-sized handle
    count, a reserved pointer, then fixed-size entries. Strided views read
    the two fields without building a Python object per handle.
    """
    pointer = ctypes.sizeof(ctypes.c_void_p)
    entry = 40 if pointer == 8 else 28
    pid_format = "Q" if pointer == 8 else "I"
    header = 2 * pointer
    count = int.from_bytes(table[:pointer], "little")
    available = (len(table) - header) // entry
    if count > available:
        raise ValueError(f"handle table claims {count} entries but holds {available}")
    entries = table[header : header + count * entry]
    # Entry layout: Object, UniqueProcessId, HandleValue (pointer-sized),
    # GrantedAccess (4), CreatorBackTraceIndex (2), ObjectTypeIndex (2), ...
    pids = entries.cast(pid_format)[1 :: entry // pointer]
    type_offset = 3 * pointer + 6
    types = entries.cast("H")[type_offset // 2 :: entry // 2]
    return pids, types


class _NtApi:
    """Thin ctypes wrapper over the two native calls classification needs."""

    def __init__(self) -> None:
        from ctypes import wintypes

        ntdll = ctypes.WinDLL("ntdll")
        self._query_system = ntdll.NtQuerySystemInformation
        self._query_system.argtypes = [
            wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)
        ]
        self._query_system.restype = wintypes.LONG
        self._query_object = ntdll.NtQueryObject
        self._query_object.argtypes = [
            wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG,
            ctypes.POINTER(wintypes.ULONG),
        ]
        self._query_object.restype = wintypes.LONG

    def handle_table(self, size: int) -> tuple[memoryview, int]:
        """Snapshot the system handle table; returns the data and buffer size."""
        from ctypes import wintypes

        while True:
            buffer = ctypes.create_string_buffer(size)
            needed = wintypes.ULONG(0)
            status = self._query_system(
                _SYSTEM_EXTENDED_HANDLE_INFORMATION, buffer, size, ctypes.byref(needed)
            ) & 0xFFFFFFFF
            if status == _STATUS_INFO_LENGTH_MISMATCH:
                # Handles come and go between calls; leave headroom.
                size = max(size * 2, needed.value + (1 << 18))
                if size > _MAX_BUFFER:
                    raise OSError("the system handle table is too large to read")
                continue
            if status != 0:
                raise OSError(f"NtQuerySystemInformation failed with status 0x{status:08X}")
            return memoryview(buffer).cast("B"), size

    def type_names(self) -> dict[int, str]:
        """Map object-type indexes to kernel type names such as ``File``."""
        from ctypes import wintypes

        size = 1 << 16
        while True:
            buffer = ctypes.create_string_buffer(size)
            needed = wintypes.ULONG(0)
            status = self._query_object(
                None, _OBJECT_TYPES_INFORMATION, buffer, size, ctypes.byref(needed)
            ) & 0xFFFFFFFF
            if status == _STATUS_INFO_LENGTH_MISMATCH:
                size = max(size * 2, needed.value + 4096)
                if size > _MAX_BUFFER:
                    raise OSError("the object type list is too large to read")
                continue
            if status != 0:
                raise OSError(f"NtQueryObject failed with status 0x{status:08X}")
            return _parse_type_names(buffer.raw, ctypes.addressof(buffer))


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("MaximumLength", ctypes.c_ushort),
        ("Buffer", ctypes.c_void_p),
    ]


class _ObjectTypeInformation(ctypes.Structure):
    _fields_ = [
        ("TypeName", _UnicodeString),
        ("TotalNumberOfObjects", ctypes.c_ulong),
        ("TotalNumberOfHandles", ctypes.c_ulong),
        ("TotalPagedPoolUsage", ctypes.c_ulong),
        ("TotalNonPagedPoolUsage", ctypes.c_ulong),
        ("TotalNamePoolUsage", ctypes.c_ulong),
        ("TotalHandleTableUsage", ctypes.c_ulong),
        ("HighWaterNumberOfObjects", ctypes.c_ulong),
        ("HighWaterNumberOfHandles", ctypes.c_ulong),
        ("HighWaterPagedPoolUsage", ctypes.c_ulong),
        ("HighWaterNonPagedPoolUsage", ctypes.c_ulong),
        ("HighWaterNamePoolUsage", ctypes.c_ulong),
        ("HighWaterHandleTableUsage", ctypes.c_ulong),
        ("InvalidAttributes", ctypes.c_ulong),
        ("GenericMapping", ctypes.c_ulong * 4),
        ("ValidAccessMask", ctypes.c_ulong),
        ("SecurityRequired", ctypes.c_ubyte),
        ("MaintainHandleCount", ctypes.c_ubyte),
        ("TypeIndex", ctypes.c_ubyte),
        ("ReservedByte", ctypes.c_ubyte),
        ("PoolType", ctypes.c_ulong),
        ("DefaultPagedPoolCharge", ctypes.c_ulong),
        ("DefaultNonPagedPoolCharge", ctypes.c_ulong),
    ]


def _parse_type_names(raw: bytes, base_address: int) -> dict[int, str]:
    """Walk ``OBJECT_TYPES_INFORMATION``; every read is bounds-checked."""
    pointer = ctypes.sizeof(ctypes.c_void_p)

    def align(value: int) -> int:
        return (value + pointer - 1) & ~(pointer - 1)

    record = ctypes.sizeof(_ObjectTypeInformation)
    count = int.from_bytes(raw[:4], "little")
    offset = align(4)
    names: dict[int, str] = {}
    for position in range(count):
        if offset + record > len(raw):
            break
        info = _ObjectTypeInformation.from_buffer_copy(raw, offset)
        start = (info.TypeName.Buffer or 0) - base_address
        end = start + info.TypeName.Length
        if 0 <= start <= end <= len(raw):
            name = raw[start:end].decode("utf-16-le", errors="replace")
            # TypeIndex exists since Windows 8.1; older systems count from 2.
            names[info.TypeIndex or position + 2] = name
        offset += record + align(info.TypeName.MaximumLength)
    return names


__all__ = [
    "CATEGORIES",
    "HandleClassifier",
    "HandleCounts",
    "describe_type_growth",
    "total_counts",
]
