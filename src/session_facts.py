"""Exact, bounded session facts kept apart from the thinned chart history.

``SnapshotHistory`` halves its resolution as a long session fills it, so a
single-sample peak or a short warning episode can fall between the
snapshots a report keeps. ``SessionFacts`` sees every snapshot instead and
keeps only fixed-size running summaries: peaks with their timestamps, the
worst severity per resource and when it was reached, signal episode
counts, how long detection actually evaluated windows, sample counts, and
the monitor's own cost. Reports export them as ``session_facts`` and
analysis prefers them over values derived from the kept snapshots.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from typing import Any

import psutil

from .events import (
    METRIC_KEYS,
    RESOURCE_METRICS,
    EvidenceState,
    LeakAlert,
    LeakResource,
    LeakSeverity,
    MetricsSnapshot,
    severity_rank,
)

# Bumped when the shape of ``session_facts`` changes incompatibly.
FACTS_SCHEMA = 1
_JUDGED = {EvidenceState.NO_GROWTH, EvidenceState.GROWTH}
_SIGNAL_METRICS = tuple(RESOURCE_METRICS.values())
_UNREADABLE_PID_LIMIT = 50
_MONITOR_ERROR_LIMIT = 20


@dataclass
class _Running:
    """Count, sum and maximum of one measurement; constant size."""

    count: int = 0
    total: float = 0.0
    peak: float | None = None

    def add(self, value: float | None) -> None:
        if value is None:
            return
        self.count += 1
        self.total += value
        self.peak = value if self.peak is None else max(self.peak, value)

    def to_dict(self, digits: int = 3) -> dict[str, Any]:
        return {
            "count": self.count,
            "mean": round(self.total / self.count, digits) if self.count else None,
            "max": round(self.peak, digits) if self.peak is not None else None,
        }


@dataclass
class _Signal:
    """One resource's signal history: worst tier, episodes, time signalled."""

    worst: LeakSeverity = LeakSeverity.NONE
    worst_at: float | None = None
    episodes: int = 0
    escalations: int = 0
    signal_seconds: float = 0.0
    # The open episode's start, last sample, and current tier.
    open_start: float | None = None
    open_last: float | None = None
    open_rank: int = 0

    def close(self) -> None:
        if self.open_start is not None and self.open_last is not None:
            self.signal_seconds += max(0.0, self.open_last - self.open_start)
        self.open_start = self.open_last = None
        self.open_rank = 0


class SessionFacts:
    """Running summaries of every snapshot a session accepts.

    Publishers update these synchronized, fixed-size summaries before queue
    eviction; direct samples use the same path. Readers receive copied state.
    This only counts detector decisions and never re-evaluates detection.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.taken = 0
        self.collected = 0
        self.measured = 0
        self.paused = 0
        self.failed = 0
        self.first_at: float | None = None
        self.last_at: float | None = None
        self._peaks: dict[str, tuple[float, float]] = {}
        self._signals = {resource: _Signal() for resource in LeakResource}
        self.after_warmup_seconds = 0.0
        self.evaluated_seconds = 0.0
        self.first_evaluated_at: float | None = None
        # The previous collected sample, unless a pause or failure intervened.
        self._previous: MetricsSnapshot | None = None
        self.requested_interval_ms: int | None = None
        self._interval = _Running()
        self._collection = _Running()
        self._monitor_cpu = _Running()
        self._classification = _Running()
        self._monitor_rss: int | None = None
        self._monitor_private: int | None = None
        self._classification_age: float | None = None
        self.dropped = 0
        self.worst = LeakSeverity.NONE
        self._latest_alerts: dict[LeakResource, LeakAlert] = {}
        self._judged_resources: set[LeakResource] = set()
        self._coverage_samples = 0
        self._fully_covered_samples = 0
        self._partial_samples = dict.fromkeys(_SIGNAL_METRICS, 0)
        self._unavailable_samples = dict.fromkeys(_SIGNAL_METRICS, 0)
        self._unreadable_pids: dict[int, str] = {}
        self._errors: list[str] = []
        self._host: dict[str, Any] | None = None

    def observe(self, snapshot: MetricsSnapshot, *, dropped: int = 0) -> None:
        """Account for one snapshot, whatever its kind."""
        with self._lock:
            self.dropped = max(self.dropped, dropped)
            self._observe(snapshot)

    def _observe(self, snapshot: MetricsSnapshot) -> None:
        self.taken += 1
        stamp = snapshot.timestamp
        self.first_at = stamp if self.first_at is None else min(self.first_at, stamp)
        self.last_at = stamp if self.last_at is None else max(self.last_at, stamp)
        self.requested_interval_ms = snapshot.sample_interval_ms
        self._observe_diagnostics(snapshot)
        if snapshot.metadata.get("sampling_error"):
            # A failed sample measured nothing; it neither clears signals nor
            # bridges the time between the samples around it.
            self.failed += 1
            error = f"sampling error: {snapshot.metadata['sampling_error']}"
            if error not in self._errors and len(self._errors) < _MONITOR_ERROR_LIMIT:
                self._errors.append(error)
            self._previous = None
            return
        if snapshot.monitoring_paused:
            self.paused += 1
            self._previous = None
            return
        self.collected += 1
        if severity_rank(snapshot.severity) > severity_rank(self.worst):
            self.worst = snapshot.severity
        self._latest_alerts.update((alert.resource, alert) for alert in snapshot.alerts)
        self._judged_resources.update(
            item.resource for item in snapshot.evidence if item.state in _JUDGED
        )
        coverage = snapshot.coverage or {}
        live_coverage = [coverage[m] for m in _SIGNAL_METRICS
                         if m in coverage and coverage[m].total > 0]
        if live_coverage:
            self._coverage_samples += 1
        for item in live_coverage:
            if item.measured < item.total:
                self._partial_samples[item.metric] += 1
            if item.measured == 0:
                self._unavailable_samples[item.metric] += 1
        unreadable = {pid for item in live_coverage for pid in item.unmeasured_pids}
        names = {process.pid: process.name for process in snapshot.processes}
        for pid in sorted(unreadable):
            if len(self._unreadable_pids) < _UNREADABLE_PID_LIMIT:
                self._unreadable_pids.setdefault(pid, names.get(pid, ""))
        previous, self._previous = self._previous, snapshot
        live = [p for p in snapshot.processes if p.status != psutil.STATUS_DEAD]
        if live:
            self.measured += 1
            if all(
                metric in coverage
                and coverage[metric].total == len(live)
                and coverage[metric].measured == len(live)
                for metric in _SIGNAL_METRICS
            ):
                self._fully_covered_samples += 1
            self._observe_peaks(snapshot, live)
        self._observe_signals(snapshot)
        if previous is None:
            return
        elapsed = max(0.0, stamp - previous.timestamp)
        self._interval.add(elapsed * 1000.0)
        if not snapshot.warming_up and not previous.warming_up:
            self.after_warmup_seconds += elapsed
        if any(item.state in _JUDGED for item in snapshot.evidence):
            self.evaluated_seconds += elapsed
            if self.first_evaluated_at is None:
                self.first_evaluated_at = previous.timestamp

    def _observe_peaks(self, snapshot: MetricsSnapshot, live: list) -> None:
        """Peaks only from totals that measured at least one live process."""
        for key in METRIC_KEYS:
            if not any(not process.access_denied and process.measured(key) for process in live):
                continue
            value = snapshot.totals.get(key)
            if value is None:
                continue
            peak = self._peaks.get(key)
            if peak is None or value > peak[0]:
                self._peaks[key] = (float(value), snapshot.timestamp)

    def _observe_signals(self, snapshot: MetricsSnapshot) -> None:
        """Episodes as reports draw them: runs of collected samples with a signal."""
        alerts = {alert.resource: alert for alert in snapshot.alerts}
        for resource, signal in self._signals.items():
            alert = alerts.get(resource)
            if alert is None or alert.severity == LeakSeverity.NONE:
                signal.close()
                continue
            rank = severity_rank(alert.severity)
            if signal.open_start is None:
                signal.episodes += 1
                signal.open_start = snapshot.timestamp
            elif rank > signal.open_rank:
                signal.escalations += 1
            signal.open_rank = rank
            signal.open_last = snapshot.timestamp
            if rank > severity_rank(signal.worst):
                signal.worst, signal.worst_at = alert.severity, snapshot.timestamp

    def _observe_diagnostics(self, snapshot: MetricsSnapshot) -> None:
        diagnostics = snapshot.diagnostics
        if diagnostics is None:
            return
        self.dropped = max(self.dropped, diagnostics.dropped_snapshots)
        if diagnostics.host is not None:
            self._host = asdict(diagnostics.host)
        if snapshot.monitoring_paused or snapshot.metadata.get("sampling_error"):
            return
        self._collection.add(diagnostics.collection_ms)
        self._monitor_cpu.add(diagnostics.monitor_cpu_percent)
        if diagnostics.monitor_rss_bytes is not None:
            self._monitor_rss = max(self._monitor_rss or 0, diagnostics.monitor_rss_bytes)
        if diagnostics.monitor_private_bytes is not None:
            self._monitor_private = max(
                self._monitor_private or 0, diagnostics.monitor_private_bytes
            )
        if snapshot.handle_types is not None:
            self._classification.add(diagnostics.handle_types_cost_ms)
        age = diagnostics.handle_types_age_seconds
        if age is not None:
            self._classification_age = max(self._classification_age or 0.0, age)

    def peak(self, key: str) -> tuple[float, float] | None:
        """``(value, timestamp)`` of a total's highest measured value."""
        with self._lock:
            return self._peaks.get(key)

    def episodes(self, resource: LeakResource) -> int:
        with self._lock:
            return self._signals[resource].episodes

    def session_state(self) -> dict[str, Any]:
        """A bounded copy of the authoritative fields used by front ends."""
        with self._lock:
            return self._session_state()

    def _session_state(self) -> dict[str, Any]:
        return {
            "samples": self.collected,
            "peaks": {key: value for key, (value, _stamp) in self._peaks.items()},
            "worst": self.worst,
            "worst_by_resource": {resource: signal.worst for resource, signal in self._signals.items()},
            "latest_alerts": dict(self._latest_alerts),
            "judged_resources": set(self._judged_resources),
            "coverage_samples": self._coverage_samples,
            "measured_samples": self.measured,
            "fully_covered_samples": self._fully_covered_samples,
            "full_coverage": self.measured > 0 and not self.failed
            and self._fully_covered_samples == self.measured,
            "partial_samples": dict(self._partial_samples),
            "unavailable_samples": dict(self._unavailable_samples),
            "unreadable_pids": dict(self._unreadable_pids),
            "errors": tuple(self._errors),
            "dropped": self.dropped,
        }

    def capture(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Capture matching bounded UI state and report facts under one lock."""
        with self._lock:
            return self._session_state(), self._to_dict()

    def to_dict(self) -> dict[str, Any]:
        """JSON primitives; episodes still open count up to the last sample."""
        with self._lock:
            return self._to_dict()

    def _to_dict(self) -> dict[str, Any]:
        signals = {}
        for resource, signal in self._signals.items():
            open_seconds = (
                max(0.0, signal.open_last - signal.open_start)
                if signal.open_start is not None and signal.open_last is not None
                else 0.0
            )
            signals[resource.value] = {
                "worst": signal.worst.value,
                "worst_at": signal.worst_at,
                "episodes": signal.episodes,
                "escalations": signal.escalations,
                "signal_seconds": round(signal.signal_seconds + open_seconds, 3),
                "active": signal.open_start is not None,
            }
        return {
            "schema": FACTS_SCHEMA,
            "samples": {
                "taken": self.taken,
                "collected": self.collected,
                "measured": self.measured,
                "paused": self.paused,
                "failed": self.failed,
            },
            "first_sample_at": self.first_at,
            "last_sample_at": self.last_at,
            "peaks": {
                key: {"value": value, "timestamp": stamp}
                for key, (value, stamp) in self._peaks.items()
            },
            "signals": signals,
            "evaluation": {
                "after_warmup_seconds": round(self.after_warmup_seconds, 3),
                "evaluated_seconds": round(self.evaluated_seconds, 3),
                "first_evaluated_at": self.first_evaluated_at,
            },
            "monitor": {
                "host_context": dict(self._host) if self._host is not None else None,
                "requested_interval_ms": self.requested_interval_ms,
                "interval_ms": self._interval.to_dict(1),
                "collection_ms": self._collection.to_dict(),
                "monitor_cpu_percent": self._monitor_cpu.to_dict(2),
                "monitor_rss_peak_bytes": self._monitor_rss,
                "monitor_private_peak_bytes": self._monitor_private,
                "handle_classification_ms": self._classification.to_dict(),
                "handle_types_max_age_seconds": (
                    round(self._classification_age, 3)
                    if self._classification_age is not None
                    else None
                ),
                "failed_samples": self.failed,
                "dropped_snapshots": self.dropped,
            },
        }


__all__ = ["FACTS_SCHEMA", "SessionFacts"]
