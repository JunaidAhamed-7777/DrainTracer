"""Small numeric helpers shared by the detector and report analysis."""

from __future__ import annotations

from collections.abc import Sequence


def least_squares_slope(times: Sequence[float], values: Sequence[float]) -> float:
    """Slope of the least-squares line through ``(time, value)`` points.

    Times are shifted so the first one is zero, which keeps the sums
    precise for Unix timestamps. Returns ``0.0`` for fewer than two points,
    mismatched lengths, or identical times.
    """
    n = len(values)
    if n < 2 or len(times) != n:
        return 0.0
    origin = times[0]
    xs = [stamp - origin for stamp in times]
    x_mean = sum(xs) / n
    y_mean = sum(values) / n
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, values))
    denominator = sum((x - x_mean) ** 2 for x in xs)
    if denominator == 0:
        return 0.0
    return numerator / denominator


__all__ = ["least_squares_slope"]
