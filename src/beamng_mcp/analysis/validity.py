"""Lap-validity gating.

The live failure: lap 6 (a 400 m loop with a full stop in it) read as the "best
lap" purely because its crossing-to-crossing time was short. In freeroam there is
no fixed track, so a raw lap time means nothing without checking the lap is a
real, continuous, representative loop. A lap is INVALID if it is too short or if
the car actually stopped during it — and an invalid lap's metrics must not be
presented as a clean hot lap.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import Sample

MIN_DISTANCE_M = 200.0
STOP_SPEED_KMH = 2.0
MAX_STOP_FRACTION = 0.05  # >5% of samples below STOP_SPEED == a real stop


@dataclass
class Validity:
    valid: bool
    distance_m: float
    n_samples: int
    stopped: bool
    stopped_fraction: float
    reasons: list[str] = field(default_factory=list)


def assess(
    samples: list[Sample],
    *,
    min_distance_m: float = MIN_DISTANCE_M,
    stop_speed_kmh: float = STOP_SPEED_KMH,
    max_stop_fraction: float = MAX_STOP_FRACTION,
) -> Validity:
    """Judge whether ``samples`` are a clean, representative lap."""
    n = len(samples)
    if n == 0:
        return Validity(False, 0.0, 0, False, 0.0, ["empty lap (no samples)"])

    distance = max(s.dist for s in samples) - min(s.dist for s in samples)
    n_stopped = sum(1 for s in samples if s.speed_kmh < stop_speed_kmh)
    stop_frac = n_stopped / n
    stopped = stop_frac > max_stop_fraction

    reasons: list[str] = []
    if distance < min_distance_m:
        reasons.append(
            f"distance {distance:.0f} m < {min_distance_m:.0f} m (not a full lap)"
        )
    if stopped:
        reasons.append(
            f"car stopped during the lap ({stop_frac * 100:.0f}% of samples below "
            f"{stop_speed_kmh:.0f} km/h) — not a clean flying lap"
        )
    return Validity(
        valid=not reasons,
        distance_m=round(distance, 1),
        n_samples=n,
        stopped=stopped,
        stopped_fraction=round(stop_frac, 3),
        reasons=reasons,
    )
