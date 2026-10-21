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
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .analysis import (
    RESOURCES,
    ReportSummary,
    ResourceSummary,
    finite,
    severity_rank,
    summarize,
)
from .handle_types import CATEGORIES as HANDLE_CATEGORIES
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
        return Path(target).name or "unknown target"

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
class ResourceComparison:
    resource: str
    before: ResourceSummary
    after: ResourceSummary
    signal: str
    trend: str

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

    @property
    def verdict(self) -> str:
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
        if not notes:
            return "no meaningful change in leak signals or drift"
        return "; ".join(notes)

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
            }

        return {
            "verdict": self.verdict,
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
                }
                for item in self.resources
            },
            "handle_type_growth": {
                category: {"before": before, "after": after}
                for category, (before, after) in self.handle_type_growth.items()
            },
            "warnings": list(self.warnings),
        }


def load_report(path: str | Path) -> LoadedReport:
    """Read a JSON report from disk and summarize it."""
    report_path = Path(path)
    try:
        text = report_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise DiffError(f"{report_path}: no such file") from None
    except UnicodeDecodeError:
        raise DiffError(f"{report_path}: not a UTF-8 JSON report") from None
    except OSError as exc:
        raise DiffError(f"{report_path}: cannot read the file ({exc.strerror or exc})") from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DiffError(f"{report_path}: not valid JSON ({exc.msg} at line {exc.lineno})") from None
    except RecursionError:
        raise DiffError(f"{report_path}: JSON nested too deeply to be a report") from None
    except ValueError as exc:  # such as an integer too long to convert
        raise DiffError(f"{report_path}: not a usable JSON report ({exc})") from None
    if not isinstance(data, dict) or not isinstance(data.get("snapshots"), list):
        raise DiffError(f"{report_path}: not a DrainTracer report (no snapshots list)")
    if not data["snapshots"]:
        raise DiffError(f"{report_path}: the report has no snapshots to compare")
    try:
        summary = summarize(data)
    except Exception as exc:  # malformed in a way the analysis did not expect
        raise DiffError(f"{report_path}: could not analyse the report ({exc})") from None
    return LoadedReport(path=report_path, data=data, summary=summary)


def compare_reports(before: LoadedReport, after: LoadedReport) -> ReportComparison:
    """Compare an earlier report with a later one."""
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
        )
        for resource in RESOURCES
    )
    return ReportComparison(
        before=before,
        after=after,
        resources=resources,
        handle_type_growth=_handle_type_growth(before.summary, after.summary),
        warnings=tuple(_comparability_warnings(before, after)),
    )


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


def _comparability_warnings(before: LoadedReport, after: LoadedReport) -> list[str]:
    warnings: list[str] = []
    if before.name != after.name:
        warnings.append(f"the reports monitored different targets ({before.name} and {after.name})")
    for label, report in (("A", before), ("B", after)):
        if not report.detection_ran:
            warnings.append(
                f"report {label} never filled a detection window, so its lack of "
                "signals is inconclusive"
            )
        partial = report.partial_resources
        if partial:
            samples = max(report.summary.resources[r].partial_samples for r in partial)
            measured = max(report.summary.resources[r].measured_samples for r in partial)
            warnings.append(
                f"report {label} could not read every process ({', '.join(partial)} totals "
                f"partial in {samples:,} of {measured:,} samples), so its totals are lower "
                "bounds and may miss growth in the unreadable processes"
            )
    differences = _settings_differences(before.data, after.data)
    if differences:
        warnings.append(
            "the runs used different settings (" + "; ".join(differences) + "), so "
            "signal severities are not directly comparable"
        )
    shorter, longer = sorted((before.summary.duration, after.summary.duration))
    if longer > 0 and shorter < longer / 2:
        warnings.append(
            f"the runs differ in length ({format_duration(before.summary.duration)} and "
            f"{format_duration(after.summary.duration)}); drift from the shorter one is "
            "less certain and long windows may not have filled"
        )
    return warnings


def _settings_differences(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    old, new = before.get("settings"), after.get("settings")
    if not isinstance(old, Mapping) or not isinstance(new, Mapping):
        if isinstance(old, Mapping) != isinstance(new, Mapping):
            return ["one report predates recorded settings"]
        return []
    differences = []

    def windows(settings: Mapping[str, Any]) -> str:
        values = settings.get("windows_seconds")
        if not isinstance(values, Sequence) or isinstance(values, str):
            return "?"
        return "/".join(format_duration(finite(value) or 0.0) for value in values)

    if windows(old) != windows(new):
        differences.append(f"windows {windows(old)} vs {windows(new)}")
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
    "DiffError",
    "IMPROVED",
    "LoadedReport",
    "MIXED",
    "REGRESSED",
    "ReportComparison",
    "ResourceComparison",
    "UNCHANGED",
    "UNKNOWN",
    "compare_reports",
    "format_amount",
    "format_drift",
    "load_report",
]
