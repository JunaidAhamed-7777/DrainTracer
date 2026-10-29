"""Conservative comparisons of bounded groups of equivalent report runs."""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from itertools import combinations
from typing import Any

from .analysis import RESOURCES, SIGNAL_TOTALS, finite, severity_rank
from .budgets import BudgetCheck, check_budgets
from .report_diff import (
    COMPARABLE,
    DRIFT_FLOOR,
    DRIFT_TOLERANCE,
    INSUFFICIENT_COVERAGE,
    NOT_COMPARABLE,
    DiffError,
    LoadedReport,
    comparability_caveats,
)

MAX_GROUP_RUNS = 20
MAX_GROUP_BYTES = 128 * 1024 * 1024
RULE = ("Median and observed min/max per side; improvement requires non-overlapping "
        "drift ranges beyond the existing noise band, or lower signals in every new run. "
        "A new strongest signal is a regression. Peaks are descriptive; budgets check "
        "the largest new-run peak. Ranges describe these runs, not confidence intervals.")


@dataclass(frozen=True)
class Spread:
    count: int
    median: float | None
    minimum: float | None
    maximum: float | None

    @classmethod
    def of(cls, values: Sequence[Any]) -> Spread:
        known = [number for value in values if (number := finite(value)) is not None]
        return cls(len(known), statistics.median(known) if known else None,
                   min(known) if known else None, max(known) if known else None)


@dataclass(frozen=True)
class RepeatedResource:
    resource: str
    drift_before: Spread
    drift_after: Spread
    peaks_before: Spread
    peaks_after: Spread
    verdict: str


@dataclass(frozen=True)
class RepeatedComparison:
    before: tuple[LoadedReport, ...]
    after: tuple[LoadedReport, ...]
    resources: tuple[RepeatedResource, ...]
    caveats: tuple[dict[str, Any], ...]
    budgets: tuple[BudgetCheck, ...] = ()

    @property
    def comparability(self) -> str:
        blocks = {item["blocks"] for item in self.caveats}
        if NOT_COMPARABLE in blocks:
            return NOT_COMPARABLE
        return INSUFFICIENT_COVERAGE if INSUFFICIENT_COVERAGE in blocks else COMPARABLE

    @property
    def blocked(self) -> bool:
        return self.comparability != COMPARABLE

    @property
    def over_budget(self) -> tuple[BudgetCheck, ...]:
        return tuple(check for check in self.budgets if check.exceeded)

    @property
    def measured_verdict(self) -> str:
        results = {resource.verdict for resource in self.resources}
        if "regressed" in results:
            return "mixed" if "improved" in results else "regressed"
        if "overlap" in results or "unknown" in results:
            return "inconclusive"
        return "improved" if "improved" in results else "unchanged"

    @property
    def verdict(self) -> str:
        return self.comparability if self.blocked else self.measured_verdict

    @property
    def verdict_label(self) -> str:
        return self.verdict + (" (qualified)" if self.caveats and not self.blocked else "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": "repeated", "rule": RULE, "verdict": self.verdict,
            "verdict_label": self.verdict_label, "measured_verdict": self.measured_verdict,
            "comparability": self.comparability, "strict": True,
            "before": [str(report.path) for report in self.before],
            "after": [str(report.path) for report in self.after],
            "resources": {item.resource: asdict(item) for item in self.resources},
            "caveats": list(self.caveats), "budgets": [check.to_dict() for check in self.budgets],
            "over_budget": bool(self.over_budget),
        }


def compare_repeated_reports(
    before: Sequence[LoadedReport], after: Sequence[LoadedReport], *,
    budgets: Mapping[str, float] | None = None,
) -> RepeatedComparison:
    """Validate every run pair before assigning a conservative grouped verdict."""
    if not 2 <= len(before) <= MAX_GROUP_RUNS or not 2 <= len(after) <= MAX_GROUP_RUNS:
        raise DiffError(f"Repeated comparisons need 2 to {MAX_GROUP_RUNS} runs on each side.")
    old, new = tuple(before), tuple(after)
    for label, reports in (("A", old), ("B", new)):
        paths = [report.path.resolve() for report in reports]
        identifiers = [value for report in reports
                       if isinstance(value := report.data.get("report_id"), str) and value]
        if len(set(paths)) != len(paths) or len(set(identifiers)) != len(identifiers):
            raise DiffError(f"Group {label} repeats a report path or report ID; collect independent runs.")
    caveats: list[dict[str, Any]] = []
    pairs = [(f"{label}{i + 1}", f"{label}{j + 1}", first, second)
             for label, reports in (("A", old), ("B", new))
             for (i, first), (j, second) in combinations(enumerate(reports), 2)]
    pairs += [(f"A{i + 1}", f"B{j + 1}", first, second)
              for i, first in enumerate(old) for j, second in enumerate(new)]
    for first_name, second_name, first, second in pairs:
        for caveat in comparability_caveats(first, second):
            caveats.append({"before": first_name, "after": second_name, **caveat.to_dict()})
    resources = []
    for resource in RESOURCES:
        drift_a = Spread.of([report.summary.resources[resource].drift_per_second for report in old])
        drift_b = Spread.of([report.summary.resources[resource].drift_per_second for report in new])
        peaks_a = Spread.of([report.summary.peaks.get(SIGNAL_TOTALS[resource]) for report in old])
        peaks_b = Spread.of([report.summary.peaks.get(SIGNAL_TOTALS[resource]) for report in new])
        if drift_a.count != len(old) or drift_b.count != len(new):
            verdict = "unknown"
            caveats.append({"before": "A", "after": "B", "kind": "missing repeated drift",
                            "message": f"{resource}: at least one run has no measured drift",
                            "blocks": INSUFFICIENT_COVERAGE})
        else:
            a = [severity_rank(report.summary.resources[resource].worst) for report in old]
            b = [severity_rank(report.summary.resources[resource].worst) for report in new]
            noise = max(DRIFT_FLOOR[resource], DRIFT_TOLERANCE * max(abs(drift_a.median), abs(drift_b.median)))
            if max(b) > max(a) or (drift_b.minimum > drift_a.maximum + noise and drift_b.minimum > 0):
                verdict = "regressed"
            elif (drift_b.maximum < drift_a.minimum - noise and drift_a.minimum > 0) or max(b) < min(a):
                verdict = "improved"
            elif abs(drift_b.median - drift_a.median) > noise or (max(b) != min(b) or max(a) != min(a)):
                verdict = "overlap"
            else:
                verdict = "unchanged"
        resources.append(RepeatedResource(resource, drift_a, drift_b, peaks_a, peaks_b, verdict))
    peaks = {metric: max((value for report in new
                         if (value := finite(report.summary.peaks.get(metric))) is not None), default=None)
             for metric in SIGNAL_TOTALS.values()}
    return RepeatedComparison(old, new, tuple(resources), tuple(caveats), check_budgets(budgets or {}, peaks))


__all__ = ["MAX_GROUP_BYTES", "MAX_GROUP_RUNS", "RULE", "RepeatedComparison", "Spread", "compare_repeated_reports"]
