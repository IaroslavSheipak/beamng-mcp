"""GT7-style racing line, drawn INTO the game world.

Renders a recorded lap's driven line as colored debug polylines (the same
world-draw API the lap timer's gates already use — no mods, no UI app).

Two colorings, both POLARITY (diverging blue <-> neutral <-> red), because on
gray tarmac a single-hue ramp doesn't read:

* ``color_by="speed"`` — RED where slow (the braking zones), blue where fast,
  around the lap's median speed — the GT7 driving-line convention.
* ``color_by="delta"`` — needs a reference lap: blue where you are GAINING
  time on the reference, red where you are LOSING it, computed from the local
  gradient of the delta-T trace (cumulative delta says "how much by here";
  the gradient says "it is happening HERE", which is what a driver can act on).

Pure half (``build_segments``) is fully testable; the drawer half talks to the
game and keeps the polyline ids so ``clear`` removes exactly what it drew.
"""

from __future__ import annotations

import bisect
import statistics

from ..analysis.model import Sample
from ..analysis.plots import delta_time
from ..errors import BeamNGError

MAX_POINTS = 350          # decimation target: ~1 point per few meters on a lap
GRADIENT_SPAN_M = 24.0    # delta gradient window (smooths sample noise)
LINE_OFFSET_M = 0.25      # draw height above ground (cling handles terrain)

# diverging poles from the reference palette: blue #2a78d6 / red #e34948,
# neutral #f0efec — as 0..1 RGBA for the debug-draw API.
_BLUE = (0.165, 0.471, 0.839)
_NEUTRAL = (0.941, 0.937, 0.925)
_RED = (0.890, 0.286, 0.282)


def _lerp(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def diverging_rgba(u: float) -> tuple:
    """u in [-1, 1] -> blue (-1) .. neutral (0) .. red (+1), alpha 1."""
    u = max(-1.0, min(1.0, u))
    rgb = _lerp(_NEUTRAL, _RED, u) if u >= 0 else _lerp(_NEUTRAL, _BLUE, -u)
    return (*rgb, 1.0)


def _bucket(u: float, buckets: int = 7) -> int:
    """Quantize u in [-1,1] into ``buckets`` bins (odd count keeps a neutral)."""
    u = max(-1.0, min(1.0, u))
    return min(buckets - 1, int((u + 1.0) / 2.0 * buckets))


def _positioned(samples: list[Sample]) -> list[Sample]:
    return [s for s in samples
            if s.posx is not None and s.posy is not None and s.posz is not None]


def _decimate(samples: list[Sample], max_points: int = MAX_POINTS) -> list[Sample]:
    if len(samples) <= max_points:
        return samples
    stride = (len(samples) + max_points - 1) // max_points
    out = samples[::stride]
    if out[-1] is not samples[-1]:
        out.append(samples[-1])
    return out


def _norm(values: list[float]) -> list[float]:
    """Center on the median, scale by the p90 magnitude -> roughly [-1, 1]."""
    if not values:
        return []
    med = statistics.median(values)
    spread = sorted(abs(v - med) for v in values)
    p90 = spread[min(len(spread) - 1, int(0.9 * len(spread)))] or 1.0
    return [(v - med) / p90 for v in values]


def _delta_gradient_at(dists: list[float], grid_d: list[float],
                       grid_delta: list[float]) -> list[float]:
    """Local d(delta)/dd (s per m) at each requested distance."""
    out: list[float] = []
    half = GRADIENT_SPAN_M / 2.0
    for d in dists:
        lo = _interp_delta(d - half, grid_d, grid_delta)
        hi = _interp_delta(d + half, grid_d, grid_delta)
        out.append((hi - lo) / GRADIENT_SPAN_M)
    return out


def _interp_delta(x: float, xs: list[float], ys: list[float]) -> float:
    i = bisect.bisect_right(xs, x)
    if i <= 0:
        return ys[0]
    if i >= len(xs):
        return ys[-1]
    x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
    return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def build_segments(samples: list[Sample], color_by: str = "speed",
                   ref: list[Sample] | None = None,
                   max_points: int = MAX_POINTS) -> list[tuple[list, tuple]]:
    """The pure half: lap samples -> [(points [[x,y,z], ...], rgba), ...].

    Consecutive points sharing a color bucket join one polyline, so the draw
    call count stays in the tens, not hundreds."""
    pts = _decimate(_positioned(samples), max_points)
    if len(pts) < 2:
        raise BeamNGError("lap has no usable position trace to draw")

    if color_by == "delta":
        if not ref:
            raise BeamNGError("color_by='delta' needs a reference lap (ref_path)")
        dt = delta_time(ref, samples)
        if not dt["d"]:
            raise BeamNGError("delta not computable between these laps: "
                              + "; ".join(dt["warnings"]))
        u = _norm(_delta_gradient_at([s.dist for s in pts], dt["d"], dt["delta"]))
    elif color_by == "speed":
        # GT7 convention: red = slow (braking zones), blue = fast
        u = [-x for x in _norm([s.speed_kmh for s in pts])]
    else:
        raise BeamNGError(f"unknown color_by {color_by!r} (use 'speed' or 'delta')")

    segments: list[tuple[list, tuple]] = []
    run: list = [[pts[0].posx, pts[0].posy, pts[0].posz]]
    run_bucket = _bucket(u[0])
    for i in range(1, len(pts)):
        b = _bucket(u[i])
        point = [pts[i].posx, pts[i].posy, pts[i].posz]
        run.append(point)
        if b != run_bucket:
            segments.append((run, diverging_rgba((run_bucket / 3.0) - 1.0)))
            run = [point]           # next run starts where this one ended
            run_bucket = b
    segments.append((run, diverging_rgba((run_bucket / 3.0) - 1.0)))
    return segments


class RacingLineDrawer:
    """Owns the drawn polyline ids so clear() removes exactly what it drew."""

    def __init__(self) -> None:
        self.ids: list[int] = []

    def draw(self, sim, segments: list[tuple[list, tuple]]) -> dict:
        sim.require_connected()
        self.clear(sim)
        drawn = 0
        with sim.lock:
            for points, rgba in segments:
                if len(points) < 2:
                    continue
                try:
                    pid = sim.bng.debug.add_polyline(
                        points, rgba, cling=True, offset=LINE_OFFSET_M)
                    self.ids.append(pid)
                    drawn += 1
                except Exception:  # noqa: BLE001 — skip a bad segment, keep going
                    continue
        if not drawn:
            raise BeamNGError("could not draw any line segment")
        return {"segments": drawn, "points": sum(len(p) for p, _ in segments)}

    def clear(self, sim) -> dict:
        removed = 0
        with sim.lock:
            for pid in self.ids:
                try:
                    sim.bng.debug.remove_polyline(pid)
                    removed += 1
                except Exception:  # noqa: BLE001
                    pass
        self.ids = []
        return {"removed": removed}
