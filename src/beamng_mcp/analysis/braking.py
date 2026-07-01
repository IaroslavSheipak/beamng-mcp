"""Braking metrics: event count, peak deceleration, and straight-line yaw
instability (yaw building while braking with the wheel ~straight = a twitchy
rear under brakes). Aggregate only — per-wheel lockup / brake-bias would need a
Lua probe and is not computed.
"""

from __future__ import annotations

from .model import Sample

BRAKE_THR = 0.05      # brake input above this == braking
STRAIGHT_STEER = 0.05  # |steering| below this == ~straight
YAW_INSTAB_RAD = 0.15  # straight-braking |yaw| above this == instability


def braking(samples: list[Sample], yaw: list[float | None]) -> dict:
    """``yaw`` is the per-sample yaw-rate series (from ``balance.yaw_rates``)."""
    events = 0
    on = False
    for s in samples:
        b = (s.brake or 0.0) > BRAKE_THR
        if b and not on:
            events += 1
        on = b

    decel = [-s.gx for s in samples if s.gx < 0]
    peak = round(max(decel), 3) if decel else 0.0

    sl_yaw = 0.0
    for i, s in enumerate(samples):
        if (
            (s.brake or 0.0) > BRAKE_THR
            and abs(s.steering or 0.0) < STRAIGHT_STEER
            and yaw[i] is not None
        ):
            sl_yaw = max(sl_yaw, abs(yaw[i]))

    return {
        "events": events,
        "peak_decel_g": peak,
        "straightline_yaw_instability": round(sl_yaw, 3),
        "unstable": sl_yaw > YAW_INSTAB_RAD,
        "note": "aggregate; per-wheel lockup / brake-bias needs a Lua probe (not computed)",
    }
