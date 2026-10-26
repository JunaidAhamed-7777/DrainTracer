"""Absolute resource budgets: stable but excessive usage is still a problem.

Leak detection looks for sustained growth, so a program that allocates
2 GB at startup and holds it never raises a signal. A budget is a fixed
ceiling on a peak total (private bytes, handles, or threads) that a run
must stay under. Headless runs and report comparisons check budgets
against peaks over the whole run, warm-up included, because excessive use
during startup is still excessive.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .events import METRIC_LABELS
from .units import format_bytes, parse_count, parse_size

# Totals a budget can cap, with the flag and config key that set each.
BUDGET_METRICS = ("private_bytes", "handles", "threads")
BUDGET_OPTIONS = {
    "private_bytes": "budget_private",
    "handles": "budget_handles",
    "threads": "budget_threads",
}


@dataclass(frozen=True)
class BudgetCheck:
    """One budget against the peak a run reached."""

    metric: str
    limit: float
    peak: float | None

    @property
    def exceeded(self) -> bool:
        return self.peak is not None and self.peak > self.limit

    @property
    def measured(self) -> bool:
        return self.peak is not None

    def describe(self) -> str:
        """``private bytes peak 612.0 MB over the 500.0 MB budget`` and similar."""
        label = METRIC_LABELS.get(self.metric, self.metric)
        limit = format_budget(self.metric, self.limit)
        if self.peak is None:
            return f"{label} budget {limit} not checked (never measured)"
        peak = format_budget(self.metric, self.peak)
        if self.exceeded:
            return f"{label} peak {peak} over the {limit} budget"
        return f"{label} peak {peak} within the {limit} budget"

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "limit": self.limit,
            "peak": self.peak,
            "exceeded": self.exceeded,
        }


def parse_budget(metric: str, value: str | int | float) -> float:
    """A budget limit: a size such as ``500MB`` for private bytes, else a whole count.

    Raises ``ValueError`` for anything that is not a positive amount, and for
    a handle or thread count with a fractional part.
    """
    if metric == "private_bytes":
        amount = parse_size(value)
    else:
        amount = parse_count(value)
        if not amount.is_integer():
            raise ValueError(f"a {metric} budget must be a whole number, got {value!r}")
    if not amount > 0:
        raise ValueError(f"a budget must be greater than zero, got {value!r}")
    return amount


def format_budget(metric: str, value: float) -> str:
    if metric == "private_bytes":
        return format_bytes(value)
    return f"{value:,.0f}"


def check_budgets(
    budgets: Mapping[str, float], peaks: Mapping[str, Any]
) -> tuple[BudgetCheck, ...]:
    """Each budget, in ``BUDGET_METRICS`` order, against the matching peak.

    A peak that is missing, not a number, or zero because nothing was ever
    measured counts as unmeasured rather than as within budget.
    """
    checks = []
    for metric in BUDGET_METRICS:
        if metric not in budgets:
            continue
        raw = peaks.get(metric)
        peak = float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None
        if peak is not None and (not math.isfinite(peak) or peak <= 0):
            peak = None
        checks.append(BudgetCheck(metric=metric, limit=float(budgets[metric]), peak=peak))
    return tuple(checks)


__all__ = [
    "BUDGET_METRICS",
    "BUDGET_OPTIONS",
    "BudgetCheck",
    "check_budgets",
    "format_budget",
    "parse_budget",
]
