"""Start/finish line geometry: a true, interpolated plane-crossing detector.

Pure math (no game), so it is fully unit-tested. Replaces v1's 10 m proximity
sphere — which false-triggered on adjacent straights/hairpins/pit lane and
quantized the lap time to the poll interval. A lap closes only when the segment
between two samples crosses the start PLANE in the forward direction (the car's
heading when the line was set), within the gate half-width, and the crossing
instant is linearly interpolated to the plane for sub-poll accuracy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: Gate half-width (m) — shared by the drawn gate and the crossing detector so
#: they can never disagree.
GATE_HALF = 6.0

Sample = tuple[float, "tuple[float, float, float] | list[float]"]


@dataclass(frozen=True)
class StartLine:
    """The start/finish line: a point + the car's forward heading when it was set."""

    pos: tuple[float, float, float] | list[float]
    heading: tuple[float, float, float] | list[float]


def line_cross(
    prev: Sample | None, cur: Sample | None, line: StartLine, gate_half: float = GATE_HALF
) -> float | None:
    """Interpolated crossing time, or None.

    ``prev``/``cur`` are ``(t, [x, y, z])`` samples (or None). Returns the time the
    car crossed the start plane if the ``prev -> cur`` segment goes from behind the
    plane (-) to ahead (+) within ``gate_half`` of the line, else None.
    """
    if prev is None or cur is None:
        return None
    p0 = line.pos
    d = line.heading
    fx, fy = d[0], d[1]
    fn = math.hypot(fx, fy) or 1.0
    fx, fy = fx / fn, fy / fn  # forward = plane normal, ground plane
    (tp, pp), (tc, pc) = prev, cur
    sp = (pp[0] - p0[0]) * fx + (pp[1] - p0[1]) * fy
    sc = (pc[0] - p0[0]) * fx + (pc[1] - p0[1]) * fy
    if sp >= 0 or sc < 0 or sc == sp:  # must go from behind (-) to ahead (+)
        return None
    frac = -sp / (sc - sp)  # interpolate to s == 0
    cx = pp[0] + frac * (pc[0] - pp[0])
    cy = pp[1] + frac * (pc[1] - pp[1])
    nx, ny = -fy, fx  # along the gate line
    lat = (cx - p0[0]) * nx + (cy - p0[1]) * ny
    if abs(lat) > gate_half:  # crossed wide of the gate — ignore
        return None
    return tp + frac * (tc - tp)


def gate_endpoints(
    line: StartLine, half: float = GATE_HALF
) -> tuple[list[float], list[float]]:
    """The two ground endpoints of the drawn gate (perpendicular to the heading)."""
    p0 = line.pos
    d = line.heading
    fx, fy = d[0], d[1]
    fn = math.hypot(fx, fy) or 1.0
    nx, ny = -fy / fn, fx / fn
    a = [p0[0] + nx * half, p0[1] + ny * half, p0[2]]
    b = [p0[0] - nx * half, p0[1] - ny * half, p0[2]]
    return a, b
