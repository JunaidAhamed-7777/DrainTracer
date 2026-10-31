"""Compare two leak reports: did a change reduce drift or resolve a leak?

Both reports go through the same analysis as the HTML report. For each
resource the comparison looks at two things:

* the strongest leak signal in each run (resolved, new, eased, worsened,
  persists, or clear), and
* the drift: the least-squares growth rate over the measured part of each
  run, after any warm-up (improved, regressed, or unchanged).

Drift differences inside a noise band (a resource-specific floor or 10% of
the larger rate, whichever is bigger) count as unchanged, and a faster
drift only counts as a regression when the newer run is actually growing.

A difference only shows that a fix helped when the runs were equivalent
experiments. Each reason they might not be is a ``Caveat``: different
targets, workloads, arguments, settings or operation counts, very different
lengths, an unjudged run, or partial coverage. Any caveat qualifies the
verdict (``improved (qualified)``), so a shorter or lighter run cannot earn
an unqualified result. In strict mode the blocking caveats replace the
verdict with ``not comparable`` or ``insufficient coverage``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .analysis import (
    RESOURCES,
    SIGNAL_TOTALS,
    ReportSummary,
    ResourceSummary,
    finite,
    severity_rank,
    summarize,
)
from .budgets import BudgetCheck, check_budgets
from .events import METRIC_LABELS
from .report_contract import ReportContractError, read_report
from .experiment import ExperimentData, load_experiment
from .handle_types import CATEGORIES as HANDLE_CATEGORIES
from .report import target_filename
from .units import format_bytes, format_duration

# Drift changes smaller than this (per second) are noise: 1 KiB/s of memory
# is about 3.5 MiB an hour; handles and threads use similar hourly scales.
DRIFT_FLOOR = {"memory": 1024.0, "handles": 0.01, "threads": 0.005}
DRIFT_TOLERANCE = 0.10

IMPROVED = "improved"
REGRESSED = "regressed"
UNCHANGED = "unchanged"
UNKNOWN = "unknown"
MIXED = "mixed"
# Strict-mode verdicts that replace the measured one.
NOT_COMPARABLE = "not comparable"
INSUFFICIENT_COVERAGE = "insufficient coverage"
COMPARABLE = "comparable"
# Operation counts further apart than this are different workloads.
OPERATIONS_TOLERANCE = 0.10


class DiffError(ValueError):
    """A report could not be loaded for comparison."""


@dataclass(frozen=True)
class LoadedReport:
    """A report read from disk with its analysis summary."""

    path: Path
    data: Mapping[str, Any]
    summary: ReportSummary

    @property
    def name(self) -> str:
        target = str(self.data.get("target_path") or "")
        return target_filename(target) or "unknown target"

    @property
    def experiment(self) -> ExperimentData | None:
        """Workload labels and provenance; ``None`` for older reports."""
        return load_experiment(self.data)

    @property
    def detection_ran(self) -> bool:
        # Reports from before v1.4 lack the field; assume detection ran.
        return self.data.get("detection_ran") is not False

    @property
    def partial_resources(self) -> tuple[str, ...]:
        """Signal resources whose totals left out unreadable processes.

        Reports from before schema 3 carry no coverage and list none.
        """
        return tuple(
            resource for resource in RESOURCES if self.summary.resources[resource].partial
        )


@dataclass(frozen=True)
class Caveat:
    """One reason two runs may not be equivalent experiments.

    ``blocks`` names the strict-mode verdict the caveat forces
    (``NOT_COMPARABLE`` or ``INSUFFICIENT_COVERAGE``), or is ``None`` for
    caveats that only qualify the result.
    """

    kind: str
    message: str
    blocks: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "message": self.message, "blocks": self.blocks}


@dataclass(frozen=True)
class ResourceComparison:
    resource: str
    before: ResourceSummary
    after: ResourceSummary
    signal: str
    trend: str
    # Whole-run peaks of the signal metric (warm-up included, exact when the
    # report has session facts): a stable but excessive total shows here
    # even when neither run grows.
    peak_before: float | None = None
    peak_after: float | None = None
    # Operation counts from the experiment labels, when the run recorded one.
    operations_before: float | None = None
    operations_after: float | None = None

    @property
    def metric(self) -> str:
        return SIGNAL_TOTALS[self.resource]

    @property
    def normalized(self) -> bool:
        """Whether both runs recorded an operation count, so growth per operation exists."""
        return self.operations_before is not None and self.operations_after is not None

    @property
    def growth_per_operation_before(self) -> float | None:
        return _per_operation(self.before.growth, self.operations_before)

    @property
    def growth_per_operation_after(self) -> float | None:
        return _per_operation(self.after.growth, self.operations_after)

    @property
    def drift_change(self) -> float | None:
        if self.before.drift_per_second is None or self.after.drift_per_second is None:
            return None
        return self.after.drift_per_second - self.before.drift_per_second

    @property
    def drift_change_ratio(self) -> float | None:
        before = self.before.drift_per_second
        change = self.drift_change
        if change is None or before is None or abs(before) < DRIFT_FLOOR[self.resource]:
            return None
        return change / abs(before)

    def change_magnitude(self) -> str:
        """Size of the drift change: ``99%``, ``40%``, or ``135x`` for big rises."""
        ratio = self.drift_change_ratio
        if ratio is None:
            return ""
        before = self.before.drift_per_second or 0.0
        after = self.after.drift_per_second or 0.0
        if before > 0 and after > 0 and after / before >= 2:
            return f"{after / before:,.0f}x"
        return f"{abs(ratio):.0%}"

    def describe_change(self) -> str:
        """Drift change in words: ``down 99%``, ``up 40%``, or ``up 135x``."""
        direction = "down" if self.trend == IMPROVED else "up"
        magnitude = self.change_magnitude()
        return f"{direction} {magnitude}" if magnitude else direction

    @property
    def improved(self) -> bool:
        return self.signal in {"resolved", "eased"} or self.trend == IMPROVED

    @property
    def regressed(self) -> bool:
        return self.signal in {"new", "worsened"} or self.trend == REGRESSED


@dataclass(frozen=True)
class ReportComparison:
    before: LoadedReport
    after: LoadedReport
    resources: tuple[ResourceComparison, ...]
    handle_type_growth: Mapping[str, tuple[int | None, int | None]]
    warnings: tuple[str, ...] = field(default_factory=tuple)
    caveats: tuple[Caveat, ...] = field(default_factory=tuple)
    # Strict comparisons refuse a measured verdict when a caveat blocks it.
    strict: bool = False
    # Absolute budgets checked against the newer run's whole-run peaks.
    budgets: tuple[BudgetCheck, ...] = field(default_factory=tuple)

    @property
    def over_budget(self) -> tuple[BudgetCheck, ...]:
        return tuple(check for check in self.budgets if check.exceeded)

    @property
    def comparability(self) -> str:
        """``comparable``, or the strict verdict the worst blocking caveat forces.

        Not comparable wins over insufficient coverage: runs of different
        experiments say nothing even with perfect coverage.
        """
        blocks = {caveat.blocks for caveat in self.caveats}
        if NOT_COMPARABLE in blocks:
            return NOT_COMPARABLE
        if INSUFFICIENT_COVERAGE in blocks:
            return INSUFFICIENT_COVERAGE
        return COMPARABLE

    @property
    def blocked(self) -> bool:
        """Whether strict mode withholds the measured verdict."""
        return self.strict and self.comparability != COMPARABLE

    @property
    def qualified(self) -> bool:
        """Whether caveats limit what the measured verdict shows."""
        return bool(self.caveats or self.warnings) and not self.blocked

    @property
    def verdict(self) -> str:
        """The measured verdict, or the strict-mode refusal that replaces it."""
        return self.comparability if self.blocked else self.measured_verdict

    @property
    def verdict_label(self) -> str:
        """The verdict as shown: ``improved (qualified)`` when caveats apply."""
        return f"{self.verdict} (qualified)" if self.qualified else self.verdict

    @property
    def measured_verdict(self) -> str:
        better = any(item.improved for item in self.resources)
        worse = any(item.regressed for item in self.resources)
        if better and worse:
            return MIXED
        if worse:
            return REGRESSED
        if better:
            return IMPROVED
        return UNCHANGED

    def headline(self) -> str:
        """One sentence naming what changed, most important first."""
        notes: list[str] = []
        for item in self.resources:
            if item.signal == "resolved":
                notes.append(f"{item.resource} leak signal resolved")
            elif item.signal == "new":
                notes.append(f"new {item.resource} leak signal ({item.after.worst.upper()})")
            elif item.signal in {"eased", "worsened"}:
                notes.append(
                    f"{item.resource} signal {item.signal} from {item.before.worst.upper()} "
                    f"to {item.after.worst.upper()}"
                )
        for item in self.resources:
            if item.trend in {IMPROVED, REGRESSED}:
                notes.append(f"{item.resource} drift {item.describe_change()}")
        text = "; ".join(notes) if notes else "no meaningful change in leak signals or drift"
        if self.over_budget:
            names = ", ".join(METRIC_LABELS.get(c.metric, c.metric) for c in self.over_budget)
            text += f"; B over budget ({names})"
        if self.blocked:
            reasons = [caveat.kind for caveat in self.caveats if caveat.blocks == self.verdict]
            return f"{self.verdict} ({', '.join(dict.fromkeys(reasons))}); measured: {text}"
        if self.qualified:
            count = len(self.caveats or self.warnings)
            text += f" (qualified by {count} caveat{'s' if count != 1 else ''})"
        return text

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready comparison for scripts and CI."""

        def side(report: LoadedReport) -> dict[str, Any]:
            return {
                "path": str(report.path),
                "report_id": report.data.get("report_id"),
                "version": report.data.get("version"),
                "target": report.name,
                "duration_seconds": round(report.summary.duration, 3),
                "worst": report.summary.worst,
                "detection_ran": report.detection_ran,
                "partial_coverage": list(report.partial_resources),
                "exact_facts": report.summary.exact,
                "chart_stride": report.summary.chart_stride,
                "experiment": _experiment_side(report.experiment),
            }

        return {
            "verdict": self.verdict,
            "verdict_label": self.verdict_label,
            "measured_verdict": self.measured_verdict,
            "qualified": self.qualified,
            "strict": self.strict,
            "comparability": self.comparability,
            "headline": self.headline(),
            "before": side(self.before),
            "after": side(self.after),
            "resources": {
                item.resource: {
                    "signal": item.signal,
                    "trend": item.trend,
                    "worst_before": item.before.worst,
                    "worst_after": item.after.worst,
                    "drift_before_per_second": item.before.drift_per_second,
                    "drift_after_per_second": item.after.drift_per_second,
                    "drift_change_ratio": item.drift_change_ratio,
                    "growth_before": item.before.growth,
                    "growth_after": item.after.growth,
                    "peak_before": item.before.peak,
                    "peak_after": item.after.peak,
                    "metric": item.metric,
                    "run_peak_before": item.peak_before,
                    "run_peak_after": item.peak_after,
                    "growth_per_operation_before": item.growth_per_operation_before,
                    "growth_per_operation_after": item.growth_per_operation_after,
                    "episodes_before": item.before.episodes,
                    "episodes_after": item.after.episodes,
                }
                for item in self.resources
            },
            "handle_type_growth": {
                category: {"before": before, "after": after}
                for category, (before, after) in self.handle_type_growth.items()
            },
            "warnings": list(self.warnings),
            "caveats": [caveat.to_dict() for caveat in self.caveats],
            "budgets": [check.to_dict() for check in self.budgets],
            "over_budget": bool(self.over_budget),
        }


def load_report(path: str | Path) -> LoadedReport:
    """Read a JSON report from disk and summarize it."""
    report_path = Path(path)
    try:
        data = read_report(report_path)
    except ReportContractError as exc:
        message = str(exc)
        if message.startswith("$"):
            message = f"not a DrainTracer report ({message})"
        raise DiffError(f"{report_path}: {message}") from None
    if not data["snapshots"]:
        raise DiffError(f"{report_path}: the report has no snapshots to compare")
    try:
        summary = summarize(data)
    except Exception as exc:  # malformed in a way the analysis did not expect
        raise DiffError(f"{report_path}: could not analyse the report ({exc})") from None
    return LoadedReport(path=report_path, data=data, summary=summary)


def _experiment_side(experiment: ExperimentData | None) -> dict[str, Any] | None:
    if experiment is None:
        return None
    return {
        "workload_id": experiment.workload_id,
        "build": experiment.build,
        "operations": experiment.operations,
        "argument_count": experiment.argument_count,
    }


def compare_reports(
    before: LoadedReport,
    after: LoadedReport,
    *,
    strict: bool = False,
    budgets: Mapping[str, float] | None = None,
) -> ReportComparison:
    """Compare an earlier report with a later one.

    With ``strict``, a caveat that makes the runs non-equivalent replaces
    the verdict with ``not comparable`` or ``insufficient coverage``.
    ``budgets`` (``{metric: limit}``) are checked against the later run's
    whole-run peaks.
    """
    old_experiment, new_experiment = before.experiment, after.experiment
    caveats = comparability_caveats(before, after)
    resources = tuple(
        ResourceComparison(
            resource=resource,
            before=before.summary.resources[resource],
            after=after.summary.resources[resource],
            signal=_signal_change(
                before.summary.resources[resource].worst, after.summary.resources[resource].worst
            ),
            trend=_drift_trend(
                resource,
                before.summary.resources[resource].drift_per_second,
                after.summary.resources[resource].drift_per_second,
            ),
            peak_before=_run_peak(before.summary, SIGNAL_TOTALS[resource]),
            peak_after=_run_peak(after.summary, SIGNAL_TOTALS[resource]),
            operations_before=old_experiment.operations if old_experiment else None,
            operations_after=new_experiment.operations if new_experiment else None,
        )
        for resource in RESOURCES
    )
    return ReportComparison(
        before=before,
        after=after,
        resources=resources,
        handle_type_growth=_handle_type_growth(before.summary, after.summary),
        warnings=tuple(caveat.message for caveat in caveats),
        caveats=tuple(caveats),
        strict=strict,
        budgets=check_budgets(budgets or {}, after.summary.peaks),
    )


def _run_peak(summary: ReportSummary, metric: str) -> float | None:
    """The whole-run peak of a total, or ``None`` when it was never measured."""
    value = finite(summary.peaks.get(metric))
    return value if value is not None and value > 0 else None


def _per_operation(growth: float | None, operations: float | None) -> float | None:
    if growth is None or not operations:
        return None
    return growth / operations


def _signal_change(before: str, after: str) -> str:
    old, new = severity_rank(before), severity_rank(after)
    if old == 0 and new == 0:
        return "clear"
    if new == 0:
        return "resolved"
    if old == 0:
        return "new"
    if new < old:
        return "eased"
    if new > old:
        return "worsened"
    return "persists"


def _drift_trend(resource: str, before: float | None, after: float | None) -> str:
    if before is None or after is None:
        return UNKNOWN
    floor = DRIFT_FLOOR[resource]
    band = max(floor, DRIFT_TOLERANCE * max(abs(before), abs(after)))
    if abs(after - before) <= band:
        return UNCHANGED
    if after < before:
        return IMPROVED
    # Faster drift only matters when the newer run actually grows.
    return REGRESSED if after > floor else UNCHANGED


def _handle_type_growth(
    before: ReportSummary, after: ReportSummary
) -> dict[str, tuple[int | None, int | None]]:
    old = before.handle_type_growth or {}
    new = after.handle_type_growth or {}
    if not before.handle_types and not after.handle_types:
        return {}
    present = [category for category in HANDLE_CATEGORIES if category in old or category in new]
    present += sorted((set(old) | set(new)) - set(HANDLE_CATEGORIES))
    return {
        category: (
            old.get(category, 0) if before.handle_types else None,
            new.get(category, 0) if after.handle_types else None,
        )
        for category in present
    }


def comparability_caveats(before: LoadedReport, after: LoadedReport) -> list[Caveat]:
    """Every reason the two runs may not be equivalent experiments, in a stable order."""
    caveats: list[Caveat] = []

    def add(kind: str, message: str, blocks: str | None = None) -> None:
        caveats.append(Caveat(kind, message, blocks))

    if before.name != after.name:
        add("target", f"the reports monitored different targets ({before.name} and {after.name})",
            NOT_COMPARABLE)
    caveats.extend(_experiment_caveats(before.experiment, after.experiment))
    for label, report in (("A", before), ("B", after)):
        if isinstance(report.data.get("privacy"), Mapping):
            add("privacy", f"report {label} was privacy transformed; original target and workload "
                "identities cannot be shown equivalent", NOT_COMPARABLE)
        recovery = report.data.get("recovery")
        if (isinstance(recovery, Mapping) and recovery.get("partial") is True
                or isinstance(report.data.get("checkpoint"), Mapping)):
            add("recovered checkpoint", f"report {label} contains a saved prefix only; its missing tail, "
                "final outcome, and cleanup are unknown", INSUFFICIENT_COVERAGE)
        if not report.detection_ran:
            add("inconclusive",
                f"report {label} never filled a detection window, so its lack of "
                "signals is inconclusive", INSUFFICIENT_COVERAGE)
        partial = report.partial_resources
        if partial:
            samples = max(report.summary.resources[r].partial_samples for r in partial)
            measured = max(report.summary.resources[r].coverage_samples for r in partial)
            add("partial coverage",
                f"report {label} could not read every process ({', '.join(partial)} totals "
                f"partial in {samples:,} of {measured:,} samples), so its totals are lower "
                "bounds and may miss growth in the unreadable processes", INSUFFICIENT_COVERAGE)
        if report.summary.chart_thinned and not report.summary.exact:
            add("thinned",
                f"report {label} keeps 1 in {report.summary.chart_stride} samples and predates "
                "exact session facts, so its peaks and episode counts may miss short events")
    differences = _settings_differences(before.data, after.data)
    if differences:
        add("settings",
            "the runs used different settings (" + "; ".join(differences) + "), so "
            "signal severities are not directly comparable", NOT_COMPARABLE)
    shorter, longer = sorted((before.summary.duration, after.summary.duration))
    if longer > 0 and shorter < longer / 2:
        add("duration",
            f"the runs differ in length ({format_duration(before.summary.duration)} and "
            f"{format_duration(after.summary.duration)}); drift from the shorter one is "
            "less certain and long windows may not have filled", NOT_COMPARABLE)
    machine = _environment_differences(before.experiment, after.experiment)
    if machine:
        old = (before.experiment.environment if before.experiment else None) or {}
        new = (after.experiment.environment if after.experiment else None) or {}
        counters_differ = any(
            old.get(key) is not None and new.get(key) is not None and old[key] != new[key]
            for key in ("os", "private_memory_metric", "handle_metric")
        )
        add("environment",
            "the runs measured different environments (" + "; ".join(machine) + "), "
            "so absolute peaks may differ for reasons other than the change",
            NOT_COMPARABLE if counters_differ else None)
    return caveats


def _experiment_caveats(
    before: ExperimentData | None, after: ExperimentData | None
) -> list[Caveat]:
    """Workload, argument and operation-count differences between the runs.

    Reports without an experiment section (older ones, or runs given no
    labels) only raise a caveat when the other run states something they
    cannot confirm.
    """
    caveats: list[Caveat] = []
    old = before or ExperimentData()
    new = after or ExperimentData()
    if old.workload_id != new.workload_id:
        if old.workload_id and new.workload_id:
            message = f"the runs used different workloads ({old.workload_id} and {new.workload_id})"
        else:
            named = old.workload_id or new.workload_id
            which = "A" if old.workload_id else "B"
            message = (
                f"only report {which} names its workload ({named}), so the runs cannot be "
                "shown to use the same one"
            )
        caveats.append(Caveat("workload", message, NOT_COMPARABLE))
    if (
        old.argument_count is not None
        and new.argument_count is not None
        and (old.argument_count, old.argument_fingerprint)
        != (new.argument_count, new.argument_fingerprint)
    ):
        caveats.append(Caveat(
            "arguments",
            f"the targets were launched with different arguments ({old.argument_count} and "
            f"{new.argument_count}, fingerprints differ)",
            NOT_COMPARABLE,
        ))
    elif (old.argument_count is None) != (new.argument_count is None):
        which = "A" if old.argument_count is not None else "B"
        caveats.append(Caveat(
            "arguments",
            f"only report {which} records target arguments, so the arguments cannot be "
            "shown to match",
            NOT_COMPARABLE,
        ))
    if old.operations is not None and new.operations is not None:
        larger = max(old.operations, new.operations)
        if abs(old.operations - new.operations) > OPERATIONS_TOLERANCE * larger:
            caveats.append(Caveat(
                "operations",
                f"the runs processed different amounts of work ({old.operations:,g} and "
                f"{new.operations:,g} operations); compare growth per operation instead",
                NOT_COMPARABLE,
            ))
    elif (old.operations is None) != (new.operations is None):
        which = "A" if old.operations is not None else "B"
        caveats.append(Caveat(
            "operations",
            f"only report {which} records an operation count, so the amount of work "
            "cannot be shown to match",
            NOT_COMPARABLE,
        ))
    return caveats


def _environment_differences(
    before: ExperimentData | None, after: ExperimentData | None
) -> list[str]:
    """Machine differences that can shift absolute peaks; empty without both records."""
    old = (before.environment if before else None) or {}
    new = (after.environment if after else None) or {}
    differences = []
    for key, label in (
        ("os", "platform"),
        ("os_release", "OS"),
        ("private_memory_metric", "private memory counter"),
        ("handle_metric", "handle counter"),
        ("cpu_count", "CPUs"),
        ("total_memory_bytes", "memory"),
        ("machine", "architecture"),
    ):
        first, second = old.get(key), new.get(key)
        if first is None or second is None or first == second:
            continue
        if key == "total_memory_bytes" and all(
            isinstance(value, (int, float)) for value in (first, second)
        ):
            differences.append(f"{label} {format_bytes(first)} vs {format_bytes(second)}")
        else:
            differences.append(f"{label} {first} vs {second}")
    return differences


def _settings_differences(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    old, new = before.get("settings"), after.get("settings")
    if not isinstance(old, Mapping) or not isinstance(new, Mapping):
        if isinstance(old, Mapping) != isinstance(new, Mapping):
            return ["one report predates recorded settings"]
        return []
    differences = []

    def windows(settings: Mapping[str, Any]) -> tuple[float | None, ...] | None:
        values = settings.get("windows_seconds")
        if not isinstance(values, Sequence) or isinstance(values, str):
            return None
        return tuple(finite(value) for value in values)

    def window_labels(values: tuple[float | None, ...] | None, precise: bool = False) -> str:
        if values is None:
            return "?"
        return "/".join(
            "?" if value is None else f"{value!r}s" if precise else format_duration(value)
            for value in values
        )

    old_windows, new_windows = windows(old), windows(new)
    if old_windows != new_windows:
        first, second = window_labels(old_windows), window_labels(new_windows)
        if first == second:
            # Duration labels round; show exact seconds when they hide the difference.
            first, second = window_labels(old_windows, True), window_labels(new_windows, True)
        differences.append(f"windows {first} vs {second}")
    for key, label in (("sample_interval_ms", "sample interval"), ("warmup_seconds", "warm-up")):
        if old.get(key) != new.get(key):
            differences.append(f"{label} {old.get(key)} vs {new.get(key)}")
    if old.get("thresholds") != new.get("thresholds"):
        differences.append("thresholds differ")
    return differences


def format_drift(resource: str, per_second: float | None) -> str:
    """Signed drift per second, or per minute when that reads better."""
    if per_second is None:
        return "—"
    sign = "-" if per_second < 0 else "+"
    magnitude = abs(per_second)
    small = magnitude < (1024 if resource == "memory" else 1.0)
    shown = magnitude * 60 if small else magnitude
    unit = "/min" if small else "/s"
    if resource == "memory":
        return f"{sign}{format_bytes(shown)}{unit}"
    return f"{sign}{shown:,.2f}{unit}"


def format_amount(resource: str, value: float | None, signed: bool = False) -> str:
    if value is None:
        return "—"
    sign = ("-" if value < 0 else "+") if signed else ""
    magnitude = abs(value) if signed else value
    if resource == "memory":
        return f"{sign}{format_bytes(magnitude)}"
    return f"{sign}{magnitude:,.0f}"


__all__ = [
    "COMPARABLE",
    "Caveat",
    "DiffError",
    "INSUFFICIENT_COVERAGE",
    "NOT_COMPARABLE",
    "IMPROVED",
    "LoadedReport",
    "MIXED",
    "REGRESSED",
    "ReportComparison",
    "ResourceComparison",
    "UNCHANGED",
    "UNKNOWN",
    "comparability_caveats",
    "compare_reports",
    "format_amount",
    "format_drift",
    "load_report",
]
