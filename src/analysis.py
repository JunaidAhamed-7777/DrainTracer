"""Summaries, alert episodes, and chart series derived from report data.

Everything here works on report dictionaries as they are written to JSON,
so fresh exports and reports loaded from disk, including older schemas,
share one code path. Missing or malformed values are skipped, never fatal.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .stats import least_squares_slope

SEVERITIES = ("none", "low", "medium", "high", "critical")
RESOURCES = ("memory", "handles", "threads")
# The totals series behind each resource's leak signal.
SIGNAL_TOTALS = {"memory": "private_bytes", "handles": "handles", "threads": "threads"}
TOTAL_KEYS = (
    "rss_bytes",
    "vms_bytes",
    "private_bytes",
    "handles",
    "threads",
    "cpu_percent",
    "io_read_bytes_per_sec",
    "io_write_bytes_per_sec",
)
# Fresh exports hold tuples (from dataclasses.asdict); JSON holds lists.
_SEQUENCES = (list, tuple)


def severity_rank(value: Any) -> int:
    """Rank from 0 (none) to 4 (critical); unknown values rank as none."""
    try:
        return SEVERITIES.index(str(value).lower())
    except ValueError:
        return 0


def finite(value: Any) -> float | None:
    """A finite float from a JSON number, or ``None`` for anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class Sample:
    """One snapshot reduced to what summaries and charts need."""

    timestamp: float
    totals: Mapping[str, float]
    alerts: Mapping[str, Mapping[str, Any]]
    process_alive: bool
    process_count: int
    paused: bool
    error: bool

    @property
    def measured(self) -> bool:
        """Whether the totals measure a live process tree."""
        return not self.paused and not self.error and self.process_count > 0


def load_samples(report: Mapping[str, Any]) -> list[Sample]:
    """Return the report's usable snapshots in time order."""
    raw = report.get("snapshots")
    if not isinstance(raw, _SEQUENCES):
        return []
    samples: list[Sample] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        timestamp = finite(item.get("timestamp"))
        if timestamp is None:
            continue
        raw_totals = item.get("totals")
        totals: dict[str, float] = {}
        if isinstance(raw_totals, Mapping):
            for key in TOTAL_KEYS:
                value = finite(raw_totals.get(key))
                if value is not None:
                    totals[key] = value
        alerts: dict[str, Mapping[str, Any]] = {}
        raw_alerts = item.get("alerts")
        for alert in raw_alerts if isinstance(raw_alerts, _SEQUENCES) else ():
            if (
                isinstance(alert, Mapping)
                and alert.get("resource") in RESOURCES
                and severity_rank(alert.get("severity")) > 0
            ):
                alerts[str(alert["resource"])] = alert
        processes = item.get("processes")
        metadata = item.get("metadata")
        samples.append(
            Sample(
                timestamp=timestamp,
                totals=totals,
                alerts=alerts,
                process_alive=bool(item.get("process_alive")),
                process_count=len(processes) if isinstance(processes, _SEQUENCES) else 0,
                paused=bool(item.get("monitoring_paused")),
                error=isinstance(metadata, Mapping) and bool(metadata.get("sampling_error")),
            )
        )
    samples.sort(key=lambda sample: sample.timestamp)
    return samples


def _signal_samples(samples: Sequence[Sample]) -> list[Sample]:
    # Paused snapshots repeat the last alerts and failed samples carry none,
    # so neither says anything new about a signal.
    return [sample for sample in samples if not sample.paused and not sample.error]


@dataclass(frozen=True)
class SignalChange:
    """A point where a resource's signal was raised or escalated."""

    resource: str
    timestamp: float
    severity: str
    kind: str
    message: str


def signal_changes(samples: Sequence[Sample]) -> list[SignalChange]:
    """Return every raise (from clear) and escalation, in time order."""
    previous = {resource: 0 for resource in RESOURCES}
    changes: list[SignalChange] = []
    for sample in _signal_samples(samples):
        for resource in RESOURCES:
            alert = sample.alerts.get(resource)
            rank = severity_rank(alert.get("severity")) if alert else 0
            if rank > previous[resource]:
                changes.append(
                    SignalChange(
                        resource=resource,
                        timestamp=sample.timestamp,
                        severity=SEVERITIES[rank],
                        kind="raised" if previous[resource] == 0 else "escalated",
                        message=str(alert.get("message", "")) if alert else "",
                    )
                )
            previous[resource] = rank
    return changes


@dataclass(frozen=True)
class AlertEpisode:
    """A continuous stretch of samples with a signal for one resource."""

    resource: str
    severity: str
    start: float
    end: float
    message: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def alert_episodes(samples: Sequence[Sample]) -> list[AlertEpisode]:
    """Group consecutive signalled samples per resource.

    Each episode keeps its strongest severity and the message from the
    first sample that reached it.
    """
    episodes: list[AlertEpisode] = []
    current: dict[str, dict[str, Any]] = {}

    def close(resource: str) -> None:
        state = current.pop(resource)
        episodes.append(
            AlertEpisode(
                resource=resource,
                severity=SEVERITIES[state["rank"]],
                start=state["start"],
                end=state["end"],
                message=state["message"],
            )
        )

    for sample in _signal_samples(samples):
        for resource in RESOURCES:
            alert = sample.alerts.get(resource)
            if alert is None:
                if resource in current:
                    close(resource)
                continue
            rank = severity_rank(alert.get("severity"))
            state = current.get(resource)
            if state is None:
                current[resource] = {
                    "rank": rank,
                    "start": sample.timestamp,
                    "end": sample.timestamp,
                    "message": str(alert.get("message", "")),
                }
                continue
            state["end"] = sample.timestamp
            if rank > state["rank"]:
                state["rank"] = rank
                state["message"] = str(alert.get("message", ""))
    for resource in list(current):
        close(resource)
    episodes.sort(key=lambda episode: (episode.start, RESOURCES.index(episode.resource)))
    return episodes


@dataclass(frozen=True)
class ResourceSummary:
    """How one signal resource behaved over the measured part of a run."""

    resource: str
    first: float | None
    last: float | None
    peak: float | None
    drift_per_second: float | None
    worst: str
    episodes: int

    @property
    def growth(self) -> float | None:
        if self.first is None or self.last is None:
            return None
        return self.last - self.first


@dataclass(frozen=True)
class ReportSummary:
    """Run-level facts used by HTML reports and report comparisons."""

    started: float | None
    ended: float | None
    sample_count: int
    max_processes: int
    worst: str
    peaks: Mapping[str, float]
    resources: Mapping[str, ResourceSummary]
    episodes: tuple[AlertEpisode, ...]
    changes: tuple[SignalChange, ...]

    @property
    def duration(self) -> float:
        if self.started is None or self.ended is None:
            return 0.0
        return max(0.0, self.ended - self.started)


def summarize(report: Mapping[str, Any], samples: Sequence[Sample] | None = None) -> ReportSummary:
    """Summarize a report; pass ``samples`` to reuse an earlier ``load_samples``."""
    samples = load_samples(report) if samples is None else samples
    measured = [sample for sample in samples if sample.measured]
    episodes = alert_episodes(samples)
    changes = signal_changes(samples)

    peaks = {
        key: max((sample.totals.get(key, 0.0) for sample in measured), default=0.0)
        for key in TOTAL_KEYS
    }
    worst_by_resource = {resource: 0 for resource in RESOURCES}
    for episode in episodes:
        rank = severity_rank(episode.severity)
        worst_by_resource[episode.resource] = max(worst_by_resource[episode.resource], rank)

    resources: dict[str, ResourceSummary] = {}
    for resource, key in SIGNAL_TOTALS.items():
        points = [(sample.timestamp, sample.totals[key]) for sample in measured if key in sample.totals]
        drift = (
            least_squares_slope([stamp for stamp, _ in points], [value for _, value in points])
            if len(points) >= 2
            else None
        )
        resources[resource] = ResourceSummary(
            resource=resource,
            first=points[0][1] if points else None,
            last=points[-1][1] if points else None,
            peak=max((value for _, value in points), default=None),
            drift_per_second=drift,
            worst=SEVERITIES[worst_by_resource[resource]],
            episodes=sum(1 for episode in episodes if episode.resource == resource),
        )

    active = [sample for sample in samples if not sample.paused]
    return ReportSummary(
        started=samples[0].timestamp if samples else None,
        ended=samples[-1].timestamp if samples else None,
        sample_count=len(active),
        max_processes=max((sample.process_count for sample in samples), default=0),
        worst=SEVERITIES[max(worst_by_resource.values(), default=0)],
        peaks=peaks,
        resources=resources,
        episodes=tuple(episodes),
        changes=tuple(changes),
    )


# Series plotted in HTML reports: payload key, totals key, decimal places.
CHART_SERIES = (
    ("private", "private_bytes", 0),
    ("rss", "rss_bytes", 0),
    ("handles", "handles", 0),
    ("threads", "threads", 0),
    ("cpu", "cpu_percent", 1),
)


def chart_payload(
    report: Mapping[str, Any],
    samples: Sequence[Sample] | None = None,
    summary: ReportSummary | None = None,
) -> dict[str, Any]:
    """Compact, JSON-ready series and annotations for the report charts.

    Times are seconds since the first sample. Values are ``None`` where the
    tree was not measured (paused, failed sample, or no live process) so
    charts break the line instead of drawing a false drop.
    """
    samples = load_samples(report) if samples is None else samples
    summary = summarize(report, samples) if summary is None else summary
    if not samples:
        return {"start": None, "t": [], "series": {}, "paused": [], "spans": [],
                "markers": [], "exits": [], "events": []}

    start = samples[0].timestamp

    def offset(stamp: float) -> float:
        return round(stamp - start, 3)

    series: dict[str, list[float | None]] = {}
    for name, key, digits in CHART_SERIES:
        values: list[float | None] = []
        for sample in samples:
            value = sample.totals.get(key) if sample.measured else None
            values.append(None if value is None else round(value, digits) if digits else round(value))
        series[name] = values

    paused: list[list[float]] = []
    for index, sample in enumerate(samples):
        if not sample.paused:
            continue
        end = samples[index + 1].timestamp if index + 1 < len(samples) else sample.timestamp
        if paused and paused[-1][1] >= offset(sample.timestamp):
            paused[-1][1] = offset(end)
        else:
            paused.append([offset(sample.timestamp), offset(end)])

    exits: list[float] = []
    for previous, sample in zip(samples, samples[1:]):
        if previous.process_alive and not sample.process_alive:
            exits.append(offset(sample.timestamp))

    events: list[dict[str, Any]] = []
    raw_events = report.get("event_log")
    for event in raw_events if isinstance(raw_events, _SEQUENCES) else ():
        if not isinstance(event, Mapping):
            continue
        stamp = finite(event.get("timestamp"))
        if stamp is None:
            continue
        events.append(
            {
                "t": offset(stamp),
                "level": str(event.get("level", "info")),
                "message": str(event.get("message", "")),
            }
        )

    return {
        "start": start,
        "t": [offset(sample.timestamp) for sample in samples],
        "series": series,
        "paused": paused,
        "spans": [
            {
                "resource": episode.resource,
                "severity": episode.severity,
                "start": offset(episode.start),
                "end": offset(episode.end),
            }
            for episode in summary.episodes
        ],
        "markers": [
            {
                "resource": change.resource,
                "t": offset(change.timestamp),
                "severity": change.severity,
                "kind": change.kind,
                "message": change.message,
            }
            for change in summary.changes
        ],
        "exits": exits,
        "events": events,
    }


__all__ = [
    "AlertEpisode",
    "CHART_SERIES",
    "RESOURCES",
    "ReportSummary",
    "ResourceSummary",
    "SEVERITIES",
    "SIGNAL_TOTALS",
    "Sample",
    "SignalChange",
    "alert_episodes",
    "chart_payload",
    "finite",
    "load_samples",
    "severity_rank",
    "signal_changes",
    "summarize",
]
