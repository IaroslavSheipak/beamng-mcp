"""Typed lap data model: a Sample is one parsed CSV row; helpers coerce safely.

Channels follow the recorder's RICH_FIELDS. G-forces are in g, in the analysis
convention (gx = longitudinal, forward +, decel -; gy = lateral; gz = vertical,
~+1 g static). speed is m/s; heading is radians.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def _f(row: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(row.get(key))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default  # None, "", or non-numeric


def _fo(row: dict, key: str) -> float | None:
    try:
        return float(row.get(key))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Sample:
    """One telemetry sample (a parsed recorder CSV row)."""

    t: float
    dist: float
    speed: float  # m/s
    posx: float | None
    posy: float | None
    posz: float | None
    heading: float  # radians
    gx: float  # longitudinal g
    gy: float  # lateral g
    gz: float  # vertical g
    steering: float | None
    throttle: float | None
    brake: float | None
    gear: float | None
    rpm: float | None
    wheelspeed: float | None

    @property
    def speed_kmh(self) -> float:
        return self.speed * 3.6

    @property
    def combined_g(self) -> float:
        """Planar (longitudinal+lateral) g magnitude — the friction-circle radius."""
        return math.hypot(self.gx, self.gy)


def sample_from_row(row: dict) -> Sample:
    """Build a Sample from a CSV row dict (missing/non-numeric -> 0 or None)."""
    return Sample(
        t=_f(row, "t"),
        dist=_f(row, "dist"),
        speed=_f(row, "speed"),
        posx=_fo(row, "posx"),
        posy=_fo(row, "posy"),
        posz=_fo(row, "posz"),
        heading=_f(row, "heading"),
        gx=_f(row, "gx"),
        gy=_f(row, "gy"),
        gz=_f(row, "gz"),
        steering=_fo(row, "steering"),
        throttle=_fo(row, "throttle"),
        brake=_fo(row, "brake"),
        gear=_fo(row, "gear"),
        rpm=_fo(row, "rpm"),
        wheelspeed=_fo(row, "wheelspeed"),
    )
