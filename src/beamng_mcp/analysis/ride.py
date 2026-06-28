"""Ride / bottoming — a gz PROXY.

The consumer build exposes no suspension-travel channel, so this degrades to a
vertical-g proxy: bottoming = gz excursions past the static baseline, plus a
gz-RMS roughness measure. Honest about being a proxy, not a damper-velocity
histogram.
"""

from __future__ import annotations

import statistics

from .model import Sample
from .util import rms

BOTTOM_THR = 0.8  # gz above baseline+this == a bottoming/kerb spike


def ride(samples: list[Sample]) -> dict:
    gz = [s.gz for s in samples]
    if not gz:
        return {"bottoming_events": 0, "gz_rms": 0.0, "settle_quality": None,
                "note": "no samples"}
    baseline = statistics.median(gz)  # ~+1 g static
    events = sum(1 for g in gz if (g - baseline) > BOTTOM_THR)
    gz_rms = rms([g - baseline for g in gz])
    settle = round(max(0.0, 1.0 - gz_rms), 3)  # lower vertical disturbance = calmer
    return {
        "bottoming_events": events,
        "gz_rms": round(gz_rms, 4),
        "settle_quality": settle,
        "note": "gz PROXY — no suspension-travel channel on the consumer build",
    }
