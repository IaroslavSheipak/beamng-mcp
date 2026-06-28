"""Small numeric helpers shared by the analysis metrics."""

from __future__ import annotations

import math
import statistics


def percentile(xs: list[float], p: float) -> float | None:
    """Linear-interpolated p-th percentile (0-100). None for an empty list."""
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return s[int(k)]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def rms(xs: list[float]) -> float:
    return math.sqrt(sum(x * x for x in xs) / len(xs)) if xs else 0.0


def mean(xs: list[float]) -> float | None:
    return statistics.mean(xs) if xs else None
