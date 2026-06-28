"""Impact-spike rejection.

The live failure: a single 16.9 g lateral "corner" (a wall strike) set the grip
envelope for a whole lap, and a 4 g spike did the same on another. A road car on
tarmac corners at ~1.2-1.5 g; anything past ~3.5 g, or a violent single-sample g
jump, is a collision/kerb impact, not grip. Flag those samples so the grip
metrics can be computed on real cornering only. Pairs with a percentile envelope
downstream (so the few remaining kerb spikes don't set the max either).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .model import Sample

IMPACT_G = 3.5  # combined g above this is a collision, not tire grip
JERK_G = 3.0  # single-sample change in the g-vector this large == an impact transient


@dataclass
class Cleaning:
    impacts: set[int]
    n_impacts: int
    n_samples: int

    @property
    def impact_fraction(self) -> float:
        return self.n_impacts / self.n_samples if self.n_samples else 0.0


def detect_impacts(
    samples: list[Sample], *, impact_g: float = IMPACT_G, jerk_g: float = JERK_G
) -> Cleaning:
    """Indices of samples that are impacts (combined g over ``impact_g`` or a
    g-vector jerk over ``jerk_g`` vs the previous sample)."""
    impacts: set[int] = set()
    prev: Sample | None = None
    for i, s in enumerate(samples):
        if s.combined_g > impact_g:
            impacts.add(i)
        if prev is not None:
            jerk = math.hypot(s.gx - prev.gx, s.gy - prev.gy)
            if jerk > jerk_g:
                impacts.add(i)
        prev = s
    return Cleaning(impacts=impacts, n_impacts=len(impacts), n_samples=len(samples))


def clean_samples(samples: list[Sample], cleaning: Cleaning) -> list[Sample]:
    """The samples with impacts removed (for grip/envelope computation)."""
    return [s for i, s in enumerate(samples) if i not in cleaning.impacts]
