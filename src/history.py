"""Session-long snapshot history for reports, thinned as it fills."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from .events import MetricsSnapshot


def _state(snapshot: MetricsSnapshot) -> tuple[Any, ...]:
    """What a report draws as a marker or span: signals, warm-up, pauses, exit, errors."""
    return (
        frozenset((alert.resource, alert.severity) for alert in snapshot.alerts),
        snapshot.warming_up,
        snapshot.monitoring_paused,
        snapshot.process_alive,
        bool(snapshot.metadata.get("sampling_error")),
    )


@dataclass(frozen=True)
class _Entry:
    index: int
    snapshot: MetricsSnapshot
    pinned: bool


class SnapshotHistory:
    """A session's snapshots for export, always reaching back to its first sample.

    At most ``limit`` snapshots are held. When the history fills, it keeps
    only the snapshots whose sample index is a multiple of twice the
    current ``stride``, and the stride doubles: the resolution halves, and
    the history still covers the whole session evenly. Snapshots where a
    signal, the warm-up, a pause, the process, or a sampling error changes
    state are pinned and survive thinning, so no alert episode disappears.
    Pins are capped at a quarter of the limit; past that, state changes
    are thinned like any other snapshot, which bounds memory even when a
    signal flaps on every sample. The latest snapshot is always included.
    """

    def __init__(self, limit: int) -> None:
        if limit < 8:
            raise ValueError("limit must be at least 8")
        self.limit = limit
        self.stride = 1
        self._entries: list[_Entry] = []
        self._pins = 0
        self._seen = 0
        self._last_state: tuple[Any, ...] | None = None
        self._latest: _Entry | None = None

    @property
    def seen(self) -> int:
        """How many snapshots were appended in total."""
        return self._seen

    def append(self, snapshot: MetricsSnapshot) -> None:
        index = self._seen
        self._seen += 1
        state = _state(snapshot)
        changed = state != self._last_state
        self._last_state = state
        pinned = changed and self._pins < self.limit // 4
        entry = _Entry(index, snapshot, pinned)
        self._latest = entry
        if not pinned and index % self.stride:
            return
        self._pins += pinned
        self._entries.append(entry)
        if len(self._entries) >= self.limit:
            self._thin()

    def _thin(self) -> None:
        """Halve the resolution; pinned snapshots and index 0 always stay."""
        self.stride *= 2
        self._entries = [
            entry for entry in self._entries if entry.pinned or entry.index % self.stride == 0
        ]

    def __len__(self) -> int:
        return len(self._entries) + (1 if self._latest_is_extra() else 0)

    def __iter__(self) -> Iterator[MetricsSnapshot]:
        for entry in self._entries:
            yield entry.snapshot
        if self._latest_is_extra():
            assert self._latest is not None
            yield self._latest.snapshot

    def _latest_is_extra(self) -> bool:
        latest = self._latest
        return latest is not None and (not self._entries or self._entries[-1] is not latest)


__all__ = ["SnapshotHistory"]
