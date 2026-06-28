"""Corner segmentation: runs of sustained lateral g, each with its minimum speed,
peak lateral g and turn direction — the "where is the lap time" view (slowest
corners are the biggest time investments).
"""

from __future__ import annotations

import statistics

from .model import Sample

CORNER_G = 0.3     # |lateral g| above this == in a corner
MIN_RUN = 3        # samples needed to count as a corner (drops noise)


def _corner(seg: list[Sample]) -> dict:
    mean_gy = statistics.mean(s.gy for s in seg)
    return {
        "dist_m": round(seg[0].dist, 1),
        "v_min_kmh": round(min(s.speed_kmh for s in seg), 1),
        "peak_lat_g": round(max(abs(s.gy) for s in seg), 3),
        # NOTE: direction label depends on the assumed gy sign convention.
        "direction": "left" if mean_gy > 0 else "right",
    }


def corners(samples: list[Sample]) -> list[dict]:
    """Detected corners, ordered by distance."""
    out: list[dict] = []
    run: list[Sample] = []
    for s in samples:
        if abs(s.gy) > CORNER_G:
            run.append(s)
        else:
            if len(run) >= MIN_RUN:
                out.append(_corner(run))
            run = []
    if len(run) >= MIN_RUN:
        out.append(_corner(run))
    return out
