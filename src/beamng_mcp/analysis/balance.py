"""Understeer/oversteer + slip — the trustworthy redesign.

v1's index was ``B = 1 - yaw_actual / yaw_neutral`` with ``yaw_neutral = v*delta/L``
and ``delta = steering * FIXED_GAIN``. The fixed steering gain was too large, so
``yaw_neutral >> yaw_actual`` and B pinned at ~+1.0 in every corner of every lap
(seen live). Two fixes:

1. **Slip angle** (course-over-ground vs heading) — calibration-free, physical,
   the trustworthy headline: ~0 when gripping, grows as the car slides. v1 never
   computed this.
2. **Self-calibrated understeer index** — estimate the steering->yaw gain from the
   car's OWN linear (low-g) regime so the neutral reference matches reality and
   the index no longer saturates. Returns ``None`` (not a forced number) when the
   lap has too little low-g data to calibrate honestly.
"""

from __future__ import annotations

import math
import statistics

from .model import Sample

G = 9.80665
WHEELBASE_M = 2.6
CORNER_ALAT_G = 0.3   # |lateral g| above this == "in a corner"
LINEAR_ALAT_G = 0.4   # |lateral g| below this == linear regime (calibration)
MIN_STEER = 0.02      # ignore near-zero steering
MIN_SPEED = 5.0       # m/s — ignore crawling
MIN_CALIB = 30        # linear-regime samples needed to trust the gain
MIN_CORNER = 20       # corner samples needed to report an index
NEUTRAL_THR = 0.08    # |index| beyond this -> a tendency word


def _wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


def yaw_rates(samples: list[Sample]) -> list[float | None]:
    """Yaw rate (rad/s) from the heading derivative, aligned to sample i."""
    n = len(samples)
    out: list[float | None] = [None] * n
    for i in range(n - 1):
        dt = samples[i + 1].t - samples[i].t
        if dt > 1e-3:
            out[i] = _wrap(samples[i + 1].heading - samples[i].heading) / dt
    return out


def slip_angles(samples: list[Sample]) -> list[float | None]:
    """Body slip angle (degrees) = course-over-ground (from the position delta)
    minus heading. Calibration-free; ~0 gripping, grows as the car slides."""
    n = len(samples)
    out: list[float | None] = [None] * n
    for i in range(n - 1):
        a, b = samples[i], samples[i + 1]
        if a.posx is None or a.posy is None or b.posx is None or b.posy is None:
            continue
        dx, dy = b.posx - a.posx, b.posy - a.posy
        if math.hypot(dx, dy) < 1e-3:
            continue
        out[i] = math.degrees(_wrap(math.atan2(dy, dx) - a.heading))
    return out


def _calibrate_gain(samples: list[Sample], yaw: list[float | None], L: float) -> tuple[float | None, int]:
    """Estimate k in ``yaw ~= k * steering * v / L`` from the linear (low-g) regime."""
    ratios: list[float] = []
    for i, s in enumerate(samples):
        if s.steering is None or yaw[i] is None:
            continue
        if abs(s.gy) > LINEAR_ALAT_G or abs(s.steering) < MIN_STEER or s.speed < MIN_SPEED:
            continue
        denom = s.steering * s.speed
        if abs(denom) > 1e-6:
            ratios.append(yaw[i] * L / denom)
    if len(ratios) < MIN_CALIB:
        return None, len(ratios)
    return statistics.median(ratios), len(ratios)


def balance(samples: list[Sample], *, wheelbase_m: float = WHEELBASE_M) -> dict:
    """Per-lap balance: slip angle (trustworthy) + a self-calibrated understeer
    index (honest null when uncalibratable)."""
    yaw = yaw_rates(samples)
    slip = slip_angles(samples)

    corner_slip = [
        abs(slip[i]) for i in range(len(samples))
        if slip[i] is not None and abs(samples[i].gy) > CORNER_ALAT_G
    ]
    corner_yaw = [
        abs(yaw[i]) for i in range(len(samples))
        if yaw[i] is not None and abs(samples[i].gy) > CORNER_ALAT_G
    ]

    k, n_calib = _calibrate_gain(samples, yaw, wheelbase_m)
    index: float | None = None
    confidence = "none"
    if k is not None and abs(k) > 1e-6:
        bs: list[float] = []
        for i, s in enumerate(samples):
            if yaw[i] is None or s.steering is None or abs(s.gy) <= CORNER_ALAT_G:
                continue
            if abs(s.steering) < MIN_STEER or s.speed < MIN_SPEED:
                continue
            yaw_ref = k * s.steering * s.speed / wheelbase_m
            if abs(yaw_ref) >= 0.05:
                bs.append(1.0 - yaw[i] / yaw_ref)
        if len(bs) >= MIN_CORNER:
            index = round(statistics.median(bs), 3)
            confidence = "medium" if n_calib >= 2 * MIN_CALIB else "low"

    tendency = "unknown"
    if index is not None:
        tendency = (
            "understeer" if index > NEUTRAL_THR
            else "oversteer (loose)" if index < -NEUTRAL_THR
            else "neutral"
        )

    return {
        "slip_angle_deg": round(statistics.mean(corner_slip), 2) if corner_slip else None,
        "peak_slip_deg": round(max(corner_slip), 2) if corner_slip else None,
        "yaw_rate_mean": round(statistics.mean(corner_yaw), 3) if corner_yaw else None,
        "understeer_index": index,
        "tendency": tendency,
        "confidence": confidence,
        "calibration_samples": n_calib,
        "note": (
            "slip_angle_deg (course vs heading) is calibration-free and trustworthy. "
            "understeer_index uses a steering->yaw gain self-calibrated from THIS lap's "
            "low-g regime; it is null when there is too little low-g data to calibrate."
        ),
    }
