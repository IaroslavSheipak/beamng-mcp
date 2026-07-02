"""Lap debrief plots — the MoTeC-style trio, as one PNG.

The three charts that answer "where is the lap time":

1. **Delta-T vs distance** (two laps) — time gained/lost by the candidate
   relative to the baseline, plotted against track distance. THE motorsport
   chart; everything else is commentary.
2. **Track map** — the driven line colored by speed (from the recorded
   ``posx/posy``), with detected corners marked by their minimum speed.
3. **Speed vs distance** — both laps overlaid on a common distance axis.

The math half (``time_over_distance``/``delta_time``) is pure and tested; the
render half lazy-imports matplotlib (Agg) so importing the package never pays
for it. Colors follow the dataviz reference palette: diverging blue<->red for
polarity (delta), a single-hue sequential blue ramp for magnitude (speed on
the map), categorical slots for lap identity.
"""

from __future__ import annotations

import os
import time

from ..timing.recorder import recent_laps
from . import corners as corners_mod
from .ingest import load_lap
from .model import Sample

#: Lap distances differing by more than this fraction => not the same circuit
#: (mirrors compare.DIST_MISMATCH — kept local so plots don't import compare).
DIST_MISMATCH = 0.10
#: Resampling step along distance, meters.
GRID_STEP_M = 2.0

# --- dataviz reference palette (light mode) ---------------------------------
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
SURFACE = "#fcfcfb"
PAGE = "#f9f9f7"
SERIES_1 = "#2a78d6"   # baseline lap (categorical slot 1, blue)
SERIES_2 = "#1baf7a"   # candidate lap (slot 2, aqua)
DIV_NEG = "#2a78d6"    # diverging pole: candidate FASTER (time gained)
DIV_POS = "#e34948"    # diverging pole: candidate slower (time lost)
THROTTLE = "#008300"   # categorical slot 4 (green) — domain-matching
BRAKE = "#e34948"      # categorical slot 6 (red)
#: Sequential blue ramp (steps 100->700) for speed-on-map magnitude.
SEQ_RAMP = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7",
            "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281",
            "#0d366b"]


# --------------------------------------------------------------------------- #
# Pure math.
# --------------------------------------------------------------------------- #
def time_over_distance(samples: list[Sample]) -> tuple[list[float], list[float]]:
    """Strictly increasing ``(dist, t)`` — stalls (no distance gained) are
    dropped so t(d) is a function and interpolation is well-defined."""
    ds: list[float] = []
    ts: list[float] = []
    last = -1.0
    for s in samples:
        if s.dist > last:
            ds.append(s.dist)
            ts.append(s.t)
            last = s.dist
    return ds, ts


def _interp(x: float, xs: list[float], ys: list[float]) -> float:
    """Linear interpolation on sorted xs (stdlib; xs len >= 2, x within range)."""
    import bisect

    i = bisect.bisect_right(xs, x)
    if i <= 0:
        return ys[0]
    if i >= len(xs):
        return ys[-1]
    x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
    if x1 == x0:
        return y0
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def delta_time(a: list[Sample], b: list[Sample], step: float = GRID_STEP_M) -> dict:
    """Candidate-minus-baseline time over a common distance grid.

    delta(d) = t_b(d) - t_a(d), re-zeroed at the grid start; negative = the
    candidate is AHEAD (gaining). Returns {d, delta, warnings}."""
    da, ta = time_over_distance(a)
    db, tb = time_over_distance(b)
    warnings: list[str] = []
    if len(da) < 2 or len(db) < 2:
        return {"d": [], "delta": [], "warnings": ["a lap has no usable distance trace"]}
    if abs(da[-1] - db[-1]) > DIST_MISMATCH * max(da[-1], db[-1]):
        warnings.append(
            f"lap distances differ by more than {DIST_MISMATCH:.0%} "
            f"({da[-1]:.0f} m vs {db[-1]:.0f} m) — delta-T is only meaningful "
            "over the shared distance; treat the tail with suspicion")
    end = min(da[-1], db[-1])
    start = max(da[0], db[0])
    if end - start < 10 * step:
        return {"d": [], "delta": [], "warnings": ["laps share too little distance"]}
    n = int((end - start) / step)
    grid = [start + i * step for i in range(n + 1)]
    raw = [_interp(d, db, tb) - _interp(d, da, ta) for d in grid]
    z = raw[0]
    return {"d": grid, "delta": [r - z for r in raw], "warnings": warnings}


MAX_CORNER_MARKERS = 8   # label only the slowest corners — the map must stay readable
CORNER_MIN_GAP_M = 30.0  # nearby detections collapse into the slower one


def corner_markers(samples: list[Sample],
                   max_markers: int = MAX_CORNER_MARKERS,
                   min_gap_m: float = CORNER_MIN_GAP_M) -> list[dict]:
    """Map positions for the corners WORTH marking: the 30 Hz detector fires on
    every sustained-g run (dozens per lap), so markers are deduped by distance
    and capped to the slowest ``max_markers`` — the biggest time investments."""
    import bisect

    ds = [s.dist for s in samples]
    slowest_first = sorted(corners_mod.corners(samples), key=lambda c: c["v_min_kmh"])
    kept: list[dict] = []
    for c in slowest_first:
        if len(kept) >= max_markers:
            break
        if any(abs(c["dist_m"] - k["dist_m"]) < min_gap_m for k in kept):
            continue
        j = min(bisect.bisect_left(ds, c["dist_m"]), len(samples) - 1)
        s = samples[j]
        if s.posx is None or s.posy is None:
            continue
        kept.append({"x": s.posx, "y": s.posy,
                     "v_min_kmh": c["v_min_kmh"], "dist_m": c["dist_m"]})
    kept.sort(key=lambda k: k["dist_m"])
    for i, k in enumerate(kept, start=1):
        k["n"] = i
    return kept


# --------------------------------------------------------------------------- #
# Rendering (lazy matplotlib import; Agg only).
# --------------------------------------------------------------------------- #
def _style_axes(ax) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
    ax.tick_params(colors=MUTED, labelsize=8)
    ax.grid(True, color=GRID, linewidth=0.7)
    ax.set_axisbelow(True)
    ax.xaxis.label.set_color(INK_2)
    ax.yaxis.label.set_color(INK_2)


def _fmt_time(s: float) -> str:
    m = int(s // 60)
    return f"{m}:{s - m * 60:06.3f}"


def _lap_duration(samples: list[Sample]) -> float:
    return samples[-1].t - samples[0].t if samples else 0.0


def render_debrief(path_a: str, path_b: str | None = None,
                   out_png: str | None = None) -> dict:
    """One debrief PNG. Two paths -> baseline-vs-candidate (map + speed +
    delta-T); one path -> single-lap (map + speed + throttle/brake)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np  # matplotlib's own hard dependency
    from matplotlib.collections import LineCollection
    from matplotlib.colors import LinearSegmentedColormap

    a = load_lap(path_a)
    if not a:
        return {"ok": False, "error": f"no usable samples in {path_a}"}
    b = load_lap(path_b) if path_b else None
    if path_b and not b:
        return {"ok": False, "error": f"no usable samples in {path_b}"}

    warnings: list[str] = []
    two = b is not None
    name_a = os.path.basename(path_a)
    name_b = os.path.basename(path_b) if path_b else None
    map_lap = b if two else a  # the lap whose line the map shows (the newest)

    fig = plt.figure(figsize=(13, 7.5), facecolor=PAGE)
    gs = fig.add_gridspec(2, 2, width_ratios=[1.05, 1.4],
                          left=0.06, right=0.97, top=0.90, bottom=0.09,
                          hspace=0.38, wspace=0.22)
    ax_map = fig.add_subplot(gs[:, 0])
    ax_speed = fig.add_subplot(gs[0, 1])
    ax_lower = fig.add_subplot(gs[1, 1], sharex=ax_speed)

    # --- track map colored by speed (sequential ramp) ------------------------
    _style_axes(ax_map)
    pts = [(s.posx, s.posy, s.speed_kmh) for s in map_lap
           if s.posx is not None and s.posy is not None]
    if len(pts) >= 2:
        cmap = LinearSegmentedColormap.from_list("seq_blue", SEQ_RAMP)
        segs = [[(pts[i][0], pts[i][1]), (pts[i + 1][0], pts[i + 1][1])]
                for i in range(len(pts) - 1)]
        speeds = [p[2] for p in pts[:-1]]
        lc = LineCollection(segs, cmap=cmap, linewidth=2.5, capstyle="round")
        lc.set_array(np.asarray(speeds))
        ax_map.add_collection(lc)
        ax_map.plot(pts[0][0], pts[0][1], marker="o", ms=8, mfc=SURFACE,
                    mec=INK, mew=1.5, zorder=5)
        ax_map.annotate("start", (pts[0][0], pts[0][1]), textcoords="offset points",
                        xytext=(8, 8), fontsize=8, color=INK_2)
        for c in corner_markers(map_lap):
            ax_map.plot(c["x"], c["y"], marker="o", ms=3.5, mfc=MUTED,
                        mec="none", zorder=4)
            ax_map.annotate(f"{c['v_min_kmh']:.0f}",
                            (c["x"], c["y"]), textcoords="offset points",
                            xytext=(5, 5), fontsize=7.5, color=INK_2)
        ax_map.autoscale()
        ax_map.set_aspect("equal", adjustable="datalim")
        cbar = fig.colorbar(lc, ax=ax_map, fraction=0.04, pad=0.02)
        cbar.set_label("km/h", color=INK_2, fontsize=8)
        cbar.ax.tick_params(colors=MUTED, labelsize=7)
        cbar.outline.set_visible(False)
    else:
        ax_map.text(0.5, 0.5, "no position trace in this lap",
                    transform=ax_map.transAxes, ha="center", color=MUTED)
        warnings.append("map skipped: no posx/posy in the lap CSV")
    ax_map.set_title(f"track map — {name_b or name_a} (speed)",
                     fontsize=10, color=INK, loc="left")

    # --- speed vs distance ----------------------------------------------------
    _style_axes(ax_speed)
    ax_speed.plot([s.dist for s in a], [s.speed_kmh for s in a],
                  color=SERIES_1, lw=2.0,
                  label=f"baseline  {name_a}  ({_fmt_time(_lap_duration(a))})")
    if two:
        ax_speed.plot([s.dist for s in b], [s.speed_kmh for s in b],
                      color=SERIES_2, lw=2.0,
                      label=f"candidate  {name_b}  ({_fmt_time(_lap_duration(b))})")
    ax_speed.set_ylabel("speed, km/h", fontsize=8)
    ax_speed.legend(loc="lower right", fontsize=7, frameon=False,
                    labelcolor=INK_2)
    ax_speed.set_title("speed vs distance", fontsize=10, color=INK, loc="left")
    plt.setp(ax_speed.get_xticklabels(), visible=False)

    # --- lower panel: delta-T (two laps) or throttle/brake (one lap) ----------
    _style_axes(ax_lower)
    ax_lower.set_xlabel("distance, m", fontsize=8)
    stats: dict = {}
    if two:
        dt = delta_time(a, b)
        warnings.extend(dt["warnings"])
        if dt["d"]:
            d, delta = dt["d"], dt["delta"]
            ax_lower.axhline(0, color=AXIS, lw=1)
            ax_lower.plot(d, delta, color=INK, lw=1.6)
            ax_lower.fill_between(d, delta, 0, where=[v <= 0 for v in delta],
                                  color=DIV_NEG, alpha=0.25, interpolate=True)
            ax_lower.fill_between(d, delta, 0, where=[v >= 0 for v in delta],
                                  color=DIV_POS, alpha=0.25, interpolate=True)
            final = delta[-1]
            stats["delta_final_s"] = round(final, 3)
            ax_lower.annotate(
                f"{final:+.2f} s", (d[-1], final), textcoords="offset points",
                xytext=(-6, 8 if final < 0 else -14), fontsize=9,
                color=DIV_NEG if final < 0 else DIV_POS, ha="right",
                fontweight="bold")
        else:
            ax_lower.text(0.5, 0.5, "delta-T not computable",
                          transform=ax_lower.transAxes, ha="center", color=MUTED)
        ax_lower.set_ylabel("delta-T, s   (below 0 = candidate ahead)", fontsize=8)
        ax_lower.set_title("delta-T vs distance  (candidate - baseline)",
                           fontsize=10, color=INK, loc="left")
    else:
        ax_lower.plot([s.dist for s in a],
                      [(s.throttle or 0.0) * 100 for s in a],
                      color=THROTTLE, lw=1.6, label="throttle %")
        ax_lower.plot([s.dist for s in a],
                      [(s.brake or 0.0) * 100 for s in a],
                      color=BRAKE, lw=1.6, label="brake %")
        ax_lower.set_ylabel("pedal, %", fontsize=8)
        ax_lower.legend(loc="upper right", fontsize=7, frameon=False,
                        labelcolor=INK_2)
        ax_lower.set_title("throttle / brake", fontsize=10, color=INK, loc="left")

    title = ("lap debrief — candidate vs baseline" if two
             else f"lap debrief — {name_a}")
    fig.suptitle(title, x=0.06, ha="left", fontsize=13, color=INK,
                 fontweight="bold")

    out = out_png or os.path.join(
        os.path.dirname(path_a), f"debrief_{int(time.time())}.png")
    fig.savefig(out, dpi=130, facecolor=PAGE)
    plt.close(fig)
    result = {"ok": True, "png": out, "warnings": warnings, "stats": stats,
              "baseline": path_a}
    if two:
        result["candidate"] = path_b
    return result


def latest_debrief_paths(logs_dir: str, path_a: str | None,
                         path_b: str | None) -> tuple[str | None, str | None, str | None]:
    """Resolve tool defaults: no args -> two newest laps (older = baseline);
    one arg -> single-lap debrief of it. Returns (a, b, error)."""
    if path_a and path_b:
        return path_a, path_b, None
    if path_a or path_b:
        return (path_a or path_b), None, None
    laps = recent_laps(logs_dir, 2)
    if not laps:
        return None, None, f"no recorded laps found in {logs_dir}"
    if len(laps) == 1:
        return laps[0], None, None
    return laps[0], laps[1], None
