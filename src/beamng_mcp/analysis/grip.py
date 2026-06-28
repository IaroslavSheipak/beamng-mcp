"""Friction-circle / grip metrics — computed on impact-CLEANED samples, with a
PERCENTILE envelope (not the raw max).

The live failure: the grip envelope was the raw max combined-g, so one wall/kerb
spike (up to 16.9 g) set it for the whole lap and made ``pct_time_near_limit``
meaningless. Here impacts are already removed upstream and the envelope is a high
percentile, so a few remaining kerb taps can't set it either.
"""

from __future__ import annotations

from .model import Sample
from .util import percentile

ENVELOPE_PCT = 98.0


def grip(cleaned: list[Sample], *, envelope_pct: float = ENVELOPE_PCT) -> dict:
    """Grip metrics on impact-cleaned samples."""
    if not cleaned:
        return {
            "max_lat_g": None, "max_accel_g": None, "max_brake_g": None,
            "envelope_g": None, "pct_time_near_limit": None,
            "note": "no clean samples to compute grip",
        }
    lat = [abs(s.gy) for s in cleaned]
    accel = [s.gx for s in cleaned if s.gx > 0]   # longitudinal +
    brake = [-s.gx for s in cleaned if s.gx < 0]  # longitudinal -
    combined = [s.combined_g for s in cleaned]
    env = percentile(combined, envelope_pct) or 0.0
    near = sum(1 for c in combined if env and c >= 0.9 * env)
    return {
        "max_lat_g": round(max(lat), 3),
        "max_accel_g": round(max(accel), 3) if accel else None,
        "max_brake_g": round(max(brake), 3) if brake else None,
        "envelope_g": round(env, 3),
        "envelope_percentile": envelope_pct,
        "pct_time_near_limit": round(100.0 * near / len(combined), 1),
        "note": (
            f"envelope = p{envelope_pct:.0f} of combined-g on impact-cleaned samples "
            "(not raw max), so kerb/impact spikes don't inflate it"
        ),
    }
