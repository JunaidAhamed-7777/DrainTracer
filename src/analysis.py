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

from .handle_types import CATEGORIES as HANDLE_CATEGORIES
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
    try:
        number = float(value)
    except OverflowError:  # a JSON integer beyond the float range
        return None
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
    handle_types: Mapping[str, int] | None = None
    warming_up: bool = False
    # (measured, total) live processes per metric; None in older reports.
    coverage: Mapping[str, tuple[int, int]] | None = None

    @property
    def measured(self) -> bool:
        """Whether the totals measure a live process tree."""
        return not self.paused and not self.error and self.process_count > 0

    def measured_metric(self, key: str) -> bool:
        """Whether ``key``'s total measures at least one live process.

        Older reports carry no coverage; their totals count as measured.
        """
        if not self.measured:
            return False
        counts = (self.coverage or {}).get(key)
        return counts is None or counts[0] > 0 or counts[1] == 0

    def partial(self, key: str) -> bool:
        """Whether ``key``'s total left out some live processes."""
        counts = (self.coverage or {}).get(key)
        return counts is not None and counts[0] < counts[1]


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
        handle_types = _handle_counts(item.get("handle_types"))
        samples.append(
            Sample(
                timestamp=timestamp,
                totals=totals,
                alerts=alerts,
                process_alive=bool(item.get("process_alive")),
                process_count=len(processes) if isinstance(processes, _SEQUENCES) else 0,
                paused=bool(item.get("monitoring_paused")),
                error=isinstance(metadata, Mapping) and bool(metadata.get("sampling_error")),
                handle_types=handle_types,
                warming_up=bool(item.get("warming_up")),
                coverage=_coverage_counts(item.get("coverage")),
            )
        )
    samples.sort(key=lambda sample: sample.timestamp)
    return samples


def _coverage_counts(value: Any) -> dict[str, tuple[int, int]] | None:
    """``(measured, total)`` per metric from a snapshot's ``coverage``, if usable."""
    if not isinstance(value, Mapping):
        return None
    counts: dict[str, tuple[int, int]] = {}
    for metric, item in value.items():
        if not isinstance(metric, str) or not isinstance(item, Mapping):
            continue
        measured, total = finite(item.get("measured")), finite(item.get("total"))
        if measured is not None and total is not None and 0 <= measured <= total:
            counts[metric] = (int(measured), int(total))
    return counts or None


def _handle_counts(value: Any) -> dict[str, int] | None:
    """Category counts from a snapshot's ``handle_types``, if it has any."""
    if not isinstance(value, Mapping):
        return None
    counts: dict[str, int] = {}
    for category, count in value.items():
        number = finite(count)
        if isinstance(category, str) and number is not None and number >= 0:
            counts[category] = int(number)
    return counts or None


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
    # Session-wide coverage, independent of retained growth/drift samples.
    partial_samples: int = 0
    measured_samples: int = 0
    coverage_samples: int = 0
    unavailable_samples: int = 0
    # From session facts when the report has them: when the worst tier was
    # first reached, and how long the signal was raised in all.
    worst_at: float | None = None
    signal_seconds: float | None = None

    @property
    def partial(self) -> bool:
        return self.partial_samples > 0

    @property
    def growth(self) -> float | None:
        if self.first is None or self.last is None:
            return None
        return self.last - self.first


@dataclass(frozen=True)
class SessionFactsData:
    """A report's ``session_facts``: exact even when the snapshots were thinned.

    Every field is optional in the JSON; missing or malformed values are
    left out so analysis falls back to what the snapshots show.
    """

    peaks: Mapping[str, tuple[float, float | None]]
    worst: Mapping[str, tuple[str, float | None]]
    episodes: Mapping[str, int]
    signal_seconds: Mapping[str, float]
    samples: Mapping[str, int]
    first_at: float | None = None
    last_at: float | None = None
    evaluated_seconds: float | None = None
    after_warmup_seconds: float | None = None
    chart_stride: int | None = None
    chart_stored: int | None = None
    chart_seen: int | None = None
    monitor: Mapping[str, Any] | None = None
    # metric -> (live-tree samples, partial samples, unavailable samples)
    coverage: Mapping[str, tuple[int, int, int]] | None = None


def _count(value: Any) -> int | None:
    number = finite(value)
    return int(number) if number is not None and number >= 0 else None


def _session_coverage(report: Mapping[str, Any]) -> dict[str, tuple[int, int, int]]:
    """Read exact coverage, falling back per metric to the older coverage block."""
    facts = report.get("session_facts")
    exact = facts.get("coverage") if isinstance(facts, Mapping) else None
    legacy = report.get("coverage")
    result = {}
    for metric in SIGNAL_TOTALS.values():
        for section, flat in ((exact, True), (legacy, False)):
            if not isinstance(section, Mapping):
                continue
            samples = _count(section.get("samples"))
            if flat:
                partial = section.get("partial_samples")
                unavailable = section.get("unavailable_samples")
                partial = _count(partial.get(metric)) if isinstance(partial, Mapping) else None
                unavailable = (
                    _count(unavailable.get(metric)) if isinstance(unavailable, Mapping) else None
                )
            else:
                metrics = section.get("metrics")
                item = metrics.get(metric) if isinstance(metrics, Mapping) else None
                partial = _count(item.get("partial_samples")) if isinstance(item, Mapping) else None
                unavailable = (
                    _count(item.get("unavailable_samples")) if isinstance(item, Mapping) else None
                )
            if (samples is not None and partial is not None and unavailable is not None
                    and unavailable <= partial <= samples):
                result[metric] = (samples, partial, unavailable)
                break
    return result


def load_facts(report: Mapping[str, Any]) -> SessionFactsData | None:
    """Parse ``session_facts`` leniently; ``None`` for older reports without them."""
    raw = report.get("session_facts")
    if not isinstance(raw, Mapping):
        return None

    def section(name: str) -> Mapping[str, Any]:
        value = raw.get(name)
        return value if isinstance(value, Mapping) else {}

    peaks: dict[str, tuple[float, float | None]] = {}
    for key, item in section("peaks").items():
        value = finite(item.get("value")) if isinstance(item, Mapping) else None
        if key in TOTAL_KEYS and value is not None:
            peaks[key] = (value, finite(item.get("timestamp")))
    worst: dict[str, tuple[str, float | None]] = {}
    episodes: dict[str, int] = {}
    signal_seconds: dict[str, float] = {}
    for resource, item in section("signals").items():
        if resource not in RESOURCES or not isinstance(item, Mapping):
            continue
        rank = severity_rank(item.get("worst"))
        worst[resource] = (SEVERITIES[rank], finite(item.get("worst_at")) if rank else None)
        count = _count(item.get("episodes"))
        if count is not None:
            episodes[resource] = count
        seconds = finite(item.get("signal_seconds"))
        if seconds is not None and seconds >= 0:
            signal_seconds[resource] = seconds
    samples: dict[str, int] = {}
    for key, value in section("samples").items():
        count = _count(value)
        if isinstance(key, str) and count is not None:
            samples[key] = count
    evaluation, chart, monitor = section("evaluation"), section("chart"), section("monitor")
    return SessionFactsData(
        peaks=peaks,
        worst=worst,
        episodes=episodes,
        signal_seconds=signal_seconds,
        samples=samples,
        first_at=finite(raw.get("first_sample_at")),
        last_at=finite(raw.get("last_sample_at")),
        evaluated_seconds=finite(evaluation.get("evaluated_seconds")),
        after_warmup_seconds=finite(evaluation.get("after_warmup_seconds")),
        chart_stride=_count(chart.get("stride")) or None,
        chart_stored=_count(chart.get("stored")),
        chart_seen=_count(chart.get("seen")),
        monitor=dict(monitor) if monitor else None,
        coverage=_session_coverage(report),
    )


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
    # Handle counts per category from the last classification, and their
    # change since the first one; None when handles were never classified.
    handle_types: Mapping[str, int] | None = None
    handle_type_growth: Mapping[str, int] | None = None
    # First and last timestamps of the warm-up, when the run had one.
    warmup: tuple[float, float] | None = None
    # Exact facts exported by the session (None for older reports), when
    # each peak in ``peaks`` was reached, and the stored chart's thinning.
    facts: SessionFactsData | None = None
    peak_times: Mapping[str, float] | None = None
    chart_stride: int = 1

    @property
    def exact(self) -> bool:
        """Whether peaks, worst tiers and episode counts come from every sample."""
        return self.facts is not None

    @property
    def chart_thinned(self) -> bool:
        return self.chart_stride > 1

    @property
    def duration(self) -> float:
        if self.started is None or self.ended is None:
            return 0.0
        return max(0.0, self.ended - self.started)

    @property
    def partial_coverage(self) -> bool:
        """Whether any signal total left out unreadable processes."""
        return any(item.partial for item in self.resources.values())


def summarize(report: Mapping[str, Any], samples: Sequence[Sample] | None = None) -> ReportSummary:
    """Summarize a report; pass ``samples`` to reuse an earlier ``load_samples``.

    Peaks, worst tiers, episode counts and the sample count come from the
    report's ``session_facts`` when present, because a thinned snapshot
    list can miss a one-sample peak or a short episode. Growth and drift
    always come from the snapshots; older reports use them for everything.
    """
    samples = load_samples(report) if samples is None else samples
    facts = load_facts(report)
    measured = [sample for sample in samples if sample.measured]
    # Growth and drift start from the post-warm-up baseline, like detection.
    judged = [sample for sample in measured if not sample.warming_up]
    episodes = alert_episodes(samples)
    changes = signal_changes(samples)

    peaks = {
        key: max(
            (sample.totals.get(key, 0.0) for sample in measured if sample.measured_metric(key)),
            default=0.0,
        )
        for key in TOTAL_KEYS
    }
    worst_by_resource = {resource: 0 for resource in RESOURCES}
    for episode in episodes:
        rank = severity_rank(episode.severity)
        worst_by_resource[episode.resource] = max(worst_by_resource[episode.resource], rank)
    peak_times: dict[str, float] | None = None
    if facts is not None:
        peak_times = {}
        for key, (value, stamp) in facts.peaks.items():
            peaks[key] = value
            if stamp is not None:
                peak_times[key] = stamp
        for resource, (severity, _stamp) in facts.worst.items():
            worst_by_resource[resource] = max(worst_by_resource[resource], severity_rank(severity))

    resources: dict[str, ResourceSummary] = {}
    coverage = facts.coverage if facts is not None else _session_coverage(report)
    for resource, key in SIGNAL_TOTALS.items():
        valid = [s for s in judged if key in s.totals and s.measured_metric(key)]
        sampled_partial = sum(1 for sample in valid if sample.partial(key))
        coverage_samples, partial, unavailable = (coverage or {}).get(key, (
            len(measured),
            sum(1 for sample in measured if sample.partial(key)),
            sum(1 for sample in measured if not sample.measured_metric(key)),
        ))
        complete = [sample for sample in valid if not sample.partial(key)]
        # A total that leaves processes out is a lower bound; mixing it with
        # complete totals would read access changes as growth or a drop.
        basis = complete if sampled_partial and len(complete) >= max(2, len(valid) / 2) else valid
        points = [(sample.timestamp, sample.totals[key]) for sample in basis]
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
            episodes=max(
                sum(1 for episode in episodes if episode.resource == resource),
                facts.episodes.get(resource, 0) if facts else 0,
            ),
            partial_samples=partial,
            measured_samples=len(valid),
            coverage_samples=coverage_samples,
            unavailable_samples=unavailable,
            worst_at=facts.worst.get(resource, ("none", None))[1] if facts else None,
            signal_seconds=facts.signal_seconds.get(resource) if facts else None,
        )

    classified = [sample for sample in judged if sample.handle_types]
    last_types = dict(classified[-1].handle_types) if classified else None
    type_growth = None
    if len(classified) >= 2:
        first_types = classified[0].handle_types or {}
        type_growth = {
            category: last_types.get(category, 0) - first_types.get(category, 0)
            for category in _ordered_categories([first_types, last_types])
            if last_types.get(category, 0) != first_types.get(category, 0)
        } or None

    active = [sample for sample in samples if not sample.paused]
    sample_count = len(active)
    started = samples[0].timestamp if samples else None
    ended = samples[-1].timestamp if samples else None
    stride = _count(report.get("snapshot_stride")) or 1
    if facts is not None:
        recorded = facts.samples.get("collected", 0) + facts.samples.get("failed", 0)
        sample_count = max(sample_count, recorded)
        if facts.first_at is not None:
            started = facts.first_at if started is None else min(started, facts.first_at)
        if facts.last_at is not None:
            ended = facts.last_at if ended is None else max(ended, facts.last_at)
        stride = max(stride, facts.chart_stride or 1)
    return ReportSummary(
        started=started,
        ended=ended,
        sample_count=sample_count,
        max_processes=max((sample.process_count for sample in samples), default=0),
        worst=SEVERITIES[max(worst_by_resource.values(), default=0)],
        peaks=peaks,
        resources=resources,
        episodes=tuple(episodes),
        changes=tuple(changes),
        handle_types=last_types,
        handle_type_growth=type_growth,
        warmup=_warmup_span(samples),
        facts=facts,
        peak_times=peak_times,
        chart_stride=stride,
    )


def _warmup_span(samples: Sequence[Sample]) -> tuple[float, float] | None:
    """From the first warming sample to the first sample after the warm-up."""
    warming = [index for index, sample in enumerate(samples) if sample.warming_up]
    if not warming:
        return None
    last = warming[-1]
    end = samples[last + 1].timestamp if last + 1 < len(samples) else samples[last].timestamp
    return samples[warming[0]].timestamp, end


def _ordered_categories(counts: Sequence[Mapping[str, int] | None]) -> list[str]:
    """Known handle categories in display order, then any others by name."""
    present = {category for mapping in counts if mapping for category in mapping}
    known = [category for category in HANDLE_CATEGORIES if category in present]
    return known + sorted(present.difference(HANDLE_CATEGORIES))


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
    tree was not measured (paused, failed sample, no live process, or no
    readable process for that metric) so charts break the line instead of
    drawing a false drop.
    """
    samples = load_samples(report) if samples is None else samples
    summary = summarize(report, samples) if summary is None else summary
    if not samples:
        return {"start": None, "t": [], "series": {}, "paused": [], "spans": [],
                "markers": [], "exits": [], "events": [], "warmup": None,
                "handle_types": {"t": [], "categories": [], "series": {}}}

    start = samples[0].timestamp

    def offset(stamp: float) -> float:
        return round(stamp - start, 3)

    series: dict[str, list[float | None]] = {}
    for name, key, digits in CHART_SERIES:
        values: list[float | None] = []
        for sample in samples:
            value = sample.totals.get(key) if sample.measured_metric(key) else None
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
        "warmup": [offset(summary.warmup[0]), offset(summary.warmup[1])] if summary.warmup else None,
        "handle_types": _handle_type_series(samples, offset),
    }


def _handle_type_series(samples: Sequence[Sample], offset) -> dict[str, Any]:
    """Handle-type counts on their own time base (classified samples only).

    Categories that never held a handle are left out; the rest are ordered
    by peak count with ``other`` last, which is also the legend order.
    """
    classified = [sample for sample in samples if sample.measured and sample.handle_types]
    categories = [
        category
        for category in _ordered_categories([sample.handle_types for sample in classified])
        if any((sample.handle_types or {}).get(category, 0) for sample in classified)
    ]
    peaks = {
        category: max((sample.handle_types or {}).get(category, 0) for sample in classified)
        for category in categories
    }
    categories.sort(key=lambda category: (category == "other", -peaks[category]))
    return {
        "t": [offset(sample.timestamp) for sample in classified],
        "categories": categories,
        "series": {
            category: [(sample.handle_types or {}).get(category, 0) for sample in classified]
            for category in categories
        },
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
    "SessionFactsData",
    "SignalChange",
    "alert_episodes",
    "chart_payload",
    "finite",
    "load_facts",
    "load_samples",
    "severity_rank",
    "signal_changes",
    "summarize",
]
