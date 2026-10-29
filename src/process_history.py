"""Bounded per-process histories, lifecycle tracking, and growth attribution.

Leak signals judge tree-wide totals, which cannot say which process grew.
``ProcessHistory`` keeps a short history per process identity (PID plus
creation time) on the detector thread, notices starts, exits and restarts,
and splits a window's growth between processes, so alerts can name the
process behind them and tell new worker capacity apart from growth inside
a process that was already running.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field

import psutil

from .events import (
    TRACKED_METRICS,
    GrowthContributor,
    LifecycleEvent,
    LifecycleKind,
    ProcessCounts,
    ProcessGrowth,
    ProcessMetrics,
    ProcessSummary,
)
from .stats import least_squares_slope

# Live processes that keep a point history; more are counted and summarized
# but cannot be attributed growth.
MAX_TRACKED_PROCESSES = 256
# Points per process; the age limit usually trims them first.
MAX_POINTS = 1024
# Exited-process summaries kept for views and reports, newest last.
MAX_EXITED = 64
# Lifecycle events carried by one snapshot; counts stay exact beyond it.
MAX_EVENTS_PER_SAMPLE = 64
# A start this soon after a same-named sibling exited is a restart.
RESTART_SECONDS = 10.0
# Contributors attached to one alert.
MAX_CONTRIBUTORS = 5
# Names the sampler uses when a process could not be read.
_PLACEHOLDER_NAMES = {"access-denied", "terminated", "unavailable", ""}
# Creation times recorded for one process agree; a reused PID differs by more.
_SAME_START = 1e-3


@dataclass
class _Track:
    pid: int
    create_time: float | None
    name: str
    ppid: int | None
    first_seen: float
    last_seen: float
    points: deque[tuple[float, tuple[float | None, ...]]] | None
    samples: int = 0
    first: dict[str, float] = field(default_factory=dict)
    last: dict[str, float] = field(default_factory=dict)
    peak: dict[str, float] = field(default_factory=dict)
    exited_at: float | None = None

    def same_process(self, process: ProcessMetrics) -> bool:
        """Whether ``process`` is this track's process, not a PID reuse.

        An unknown creation time on either side cannot prove a reuse.
        """
        if self.create_time is None or process.create_time is None:
            return True
        return abs(self.create_time - process.create_time) <= _SAME_START

    def record(self, now: float, process: ProcessMetrics, max_age: float) -> None:
        self.last_seen = now
        self.samples += 1
        if self.create_time is None:
            self.create_time = process.create_time
        if self.name in _PLACEHOLDER_NAMES and process.name not in _PLACEHOLDER_NAMES:
            self.name = process.name
        if process.ppid is not None:
            self.ppid = process.ppid
        values: list[float | None] = []
        for metric in TRACKED_METRICS:
            if not process.measured(metric) or process.access_denied:
                values.append(None)
                continue
            value = float(getattr(process, metric))
            values.append(value)
            self.first.setdefault(metric, value)
            self.last[metric] = value
            self.peak[metric] = max(self.peak.get(metric, value), value)
        if self.points is not None:
            self.points.append((now, tuple(values)))
            cutoff = now - max_age
            while self.points and self.points[0][0] < cutoff:
                self.points.popleft()

    def summary(self) -> ProcessSummary:
        return ProcessSummary(
            pid=self.pid,
            create_time=self.create_time,
            name=self.name,
            ppid=self.ppid,
            first_seen=self.first_seen,
            last_seen=self.last_seen,
            samples=self.samples,
            first=dict(self.first),
            last=dict(self.last),
            peak=dict(self.peak),
            exited_at=self.exited_at,
        )


@dataclass(frozen=True)
class Attribution:
    """How a window's growth in one metric splits between processes."""

    contributors: tuple[GrowthContributor, ...]
    growth_from_new_processes: float
    growth_within_existing: float
    # Processes started inside the window whose arrival added to the total.
    new_processes: int = 0


class ProcessHistory:
    """Per-process histories for one monitored tree, owned by the detector thread.

    Every method takes a lock, so a front end may read ``points`` for one
    identity while sampling continues.
    """

    def __init__(
        self,
        max_age_seconds: float,
        max_processes: int = MAX_TRACKED_PROCESSES,
        max_exited: int = MAX_EXITED,
    ) -> None:
        self.max_age_seconds = max_age_seconds
        self.max_processes = max_processes
        self._lock = threading.Lock()
        self._live: dict[int, _Track] = {}
        # Exited tracks that still hold points a window may need.
        self._departed: deque[_Track] = deque(maxlen=max_exited)
        self._exited: deque[ProcessSummary] = deque(maxlen=max_exited)
        self._exited_view: tuple[ProcessSummary, ...] = ()
        self._restart_candidates: deque[ProcessSummary] = deque(maxlen=max_exited)
        self._observed = False
        self._started = 0
        self._exited_count = 0
        self._restarted = 0

    @property
    def counts(self) -> ProcessCounts:
        with self._lock:
            return self._counts()

    def _counts(self) -> ProcessCounts:
        return ProcessCounts(
            live=len(self._live),
            started=self._started,
            exited=self._exited_count,
            restarted=self._restarted,
        )

    def observe(self, now: float, processes: Iterable[ProcessMetrics]) -> tuple[LifecycleEvent, ...]:
        """Record one sample of the tree; return its starts, exits and restarts.

        The first sample sets the starting tree and reports no events.
        Processes reported as dead have exited.
        """
        with self._lock:
            live = [p for p in processes if p.status != psutil.STATUS_DEAD]
            first = not self._observed
            self._observed = True
            exits: list[LifecycleEvent] = []
            starts: list[ProcessMetrics] = []
            seen = {process.pid for process in live}
            for pid in [pid for pid in self._live if pid not in seen]:
                exits.append(self._retire(self._live[pid], now))
            with_points = sum(item.points is not None for item in self._live.values())
            for process in live:
                track = self._live.get(process.pid)
                if track is not None and not track.same_process(process):
                    with_points -= track.points is not None
                    exits.append(self._retire(track, now))
                    track = None
                if track is None:
                    track = self._new_track(now, process)
                    self._live[process.pid] = track
                    with_points += track.points is not None
                    if not first:
                        starts.append(process)
                elif track.points is None and with_points < self.max_processes:
                    # Overflow tracks may begin a history once a live slot is
                    # free. Earlier observations remain summary-only.
                    track.points = deque(maxlen=MAX_POINTS)
                    with_points += 1
                track.record(now, process, self.max_age_seconds)
            self._prune_departed(now)
            while self._restart_candidates and (
                now - self._restart_candidates[0].exited_at > RESTART_SECONDS
            ):
                self._restart_candidates.popleft()
            events = exits + [self._start_event(now, process) for process in starts]
            return tuple(events[:MAX_EVENTS_PER_SAMPLE])

    def _new_track(self, now: float, process: ProcessMetrics) -> _Track:
        with_points = sum(1 for t in self._live.values() if t.points is not None)
        points = deque(maxlen=MAX_POINTS) if with_points < self.max_processes else None
        return _Track(
            pid=process.pid,
            create_time=process.create_time,
            name=process.name if process.name not in _PLACEHOLDER_NAMES else "",
            ppid=process.ppid,
            first_seen=now,
            last_seen=now,
            points=points,
        )

    def _retire(self, track: _Track, now: float) -> LifecycleEvent:
        del self._live[track.pid]
        # The process was last seen alive at ``last_seen``; it ended by ``now``.
        track.exited_at = now
        self._exited_count += 1
        summary = track.summary()
        self._exited.append(summary)
        self._restart_candidates.append(summary)
        self._exited_view = tuple(self._exited)
        if track.points is not None:
            self._departed.append(track)
        return LifecycleEvent(
            kind=LifecycleKind.EXITED,
            timestamp=now,
            pid=track.pid,
            name=track.name or "unknown",
            create_time=track.create_time,
            ppid=track.ppid,
        )

    def _start_event(self, now: float, process: ProcessMetrics) -> LifecycleEvent:
        self._started += 1
        name = self._live[process.pid].name
        replaced = self._replaced(now, name, process.ppid)
        if replaced is not None:
            self._restarted += 1
        return LifecycleEvent(
            kind=LifecycleKind.RESTARTED if replaced is not None else LifecycleKind.STARTED,
            timestamp=now,
            pid=process.pid,
            name=name or "unknown",
            create_time=process.create_time,
            ppid=process.ppid,
            replaces_pid=replaced.pid if replaced is not None else None,
        )

    def _replaced(
        self, now: float, name: str, ppid: int | None,
    ) -> ProcessSummary | None:
        """The latest same-named sibling that exited within ``RESTART_SECONDS``.

        Remove each matched exit occurrence so later samples cannot consume it again.
        """
        if not name:
            return None
        for index in range(len(self._restart_candidates) - 1, -1, -1):
            summary = self._restart_candidates[index]
            if summary.exited_at is None or now - summary.exited_at > RESTART_SECONDS:
                break
            if summary.name == name and summary.ppid == ppid:
                del self._restart_candidates[index]
                return summary
        return None

    def _prune_departed(self, now: float) -> None:
        cutoff = now - self.max_age_seconds
        while self._departed and (self._departed[0].exited_at or now) < cutoff:
            self._departed.popleft()

    def summaries(self) -> tuple[ProcessSummary, ...]:
        """One summary per live process, ordered by PID."""
        with self._lock:
            return tuple(self._live[pid].summary() for pid in sorted(self._live))

    def exited(self) -> tuple[ProcessSummary, ...]:
        """The most recent exited processes, oldest first (bounded)."""
        with self._lock:
            return self._exited_view

    def points(self, identity: tuple[int, float | None]) -> tuple[tuple[float, dict[str, float]], ...]:
        """Recent ``(timestamp, {metric: value})`` points of one process identity.

        Unmeasured values are left out of each point's mapping. Returns an
        empty tuple for identities without a point history.
        """
        pid, created = identity
        with self._lock:
            candidates = [self._live.get(pid), *reversed(self._departed)]
            for track in candidates:
                if track is None or track.pid != pid or track.points is None:
                    continue
                if created is not None and track.create_time is not None and (
                    abs(track.create_time - created) > _SAME_START
                ):
                    continue
                return tuple(
                    (
                        stamp,
                        {m: v for m, v in zip(TRACKED_METRICS, values) if v is not None},
                    )
                    for stamp, values in track.points
                )
            return ()

    def growth(
        self, metric: str, since: float, until: float | None = None
    ) -> tuple[ProcessGrowth, ...]:
        """Each process's signed change and slope of ``metric`` over ``[since, until]``.

        Covers live processes and recently exited ones that still hold
        points, ordered by PID. Unlike ``attribute`` this compares measured
        values only (a new process grows from its first sample), which is
        what a per-process inspector shows. ``until`` defaults to now.
        """
        index = TRACKED_METRICS.index(metric)
        with self._lock:
            tracks = [*self._live.values(), *self._departed]
            rows: list[ProcessGrowth] = []
            for track in tracks:
                if track.points is None:
                    continue
                values = [
                    (stamp, point[index])
                    for stamp, point in track.points
                    if stamp >= since and (until is None or stamp <= until)
                    and point[index] is not None
                ]
                if not values:
                    continue
                times = [stamp for stamp, _value in values]
                series = [float(value) for _stamp, value in values]
                rows.append(
                    ProcessGrowth(
                        pid=track.pid,
                        create_time=track.create_time,
                        name=track.name or "unknown",
                        ppid=track.ppid,
                        metric=metric,
                        first_value=series[0],
                        last_value=series[-1],
                        delta=series[-1] - series[0],
                        slope_per_second=least_squares_slope(times, series),
                        samples=len(series),
                        span_seconds=times[-1] - times[0],
                        new_process=track.first_seen > since,
                        exited=track.exited_at is not None,
                    )
                )
        return tuple(sorted(rows, key=lambda row: (row.pid, row.create_time or 0.0)))

    def attribute(self, metric: str, since: float, until: float) -> Attribution:
        """Split growth of ``metric`` over ``[since, until]`` between processes.

        A process that ran at ``since`` contributes its last value in the
        window minus its first; one started later contributes its whole
        value (it added to the total by arriving); one that exited
        contributes minus its first value, since its resources left the
        total. Shares are of the positive growth only.
        """
        index = TRACKED_METRICS.index(metric)
        with self._lock:
            tracks = [*self._live.values(), *self._departed]
            changes: list[tuple[_Track, float, bool, bool]] = []
            for track in tracks:
                if track.points is None:
                    continue
                values = [
                    (stamp, point[index])
                    for stamp, point in track.points
                    if since <= stamp <= until and point[index] is not None
                ]
                if not values:
                    continue
                new = track.first_seen > since
                exited = track.exited_at is not None and track.exited_at <= until
                baseline = 0.0 if new else values[0][1]
                end = 0.0 if exited else values[-1][1]
                changes.append((track, end - baseline, new, exited))
        growth_new = sum(delta for _t, delta, new, _e in changes if new and delta > 0)
        growth_existing = sum(delta for _t, delta, new, _e in changes if not new and delta > 0)
        positive = growth_new + growth_existing
        ranked = sorted(
            (change for change in changes if change[1] > 0),
            key=lambda change: (-change[1], change[0].pid),
        )[:MAX_CONTRIBUTORS]
        contributors = tuple(
            GrowthContributor(
                pid=track.pid,
                create_time=track.create_time,
                name=track.name or "unknown",
                delta=delta,
                share=delta / positive if positive else 0.0,
                new_process=new,
                exited=exited,
            )
            for track, delta, new, exited in ranked
        )
        new_count = sum(1 for _t, delta, new, _e in changes if new and delta > 0)
        return Attribution(contributors, growth_new, growth_existing, new_count)


__all__ = [
    "MAX_CONTRIBUTORS",
    "MAX_EXITED",
    "MAX_TRACKED_PROCESSES",
    "Attribution",
    "ProcessHistory",
]
