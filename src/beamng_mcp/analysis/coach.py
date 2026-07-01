"""Driving-technique coach — separates the DRIVER from the CAR.

The race engineer (``engineer/``) changes the car; the coach reads the same
lap and tells the driver what to change. Every tip is grounded in a measured
channel and carries the same confidence labels as the engineer plan, with
honest caveats (normalized steering, gz proxies). Where the evidence points at
the car instead of the driver, the tip says so and hands off to the engineer.
"""

from __future__ import annotations

from .model import Sample

# -- thresholds (documented, conservative — a tip must survive skepticism) ----
COAST_THROTTLE = 0.10     # below this throttle...
COAST_BRAKE = 0.05        # ...and below this brake == coasting
COAST_MIN_KMH = 30.0      # only count coasting above town speed
COAST_PCT_TIP = 12.0      # coasting more than this % of the lap draws a tip
BRAKE_UTIL_TIP = 0.75     # peak brake g under this share of the envelope draws a tip
MIN_ENVELOPE_G = 0.5      # don't reason about utilization on a cruise lap
CORNER_UTIL_TIP = 0.80    # corner peak lat g under this share == under-driven
FULL_THROTTLE = 0.90      # throttle at/above this == committed
SAW_G = 0.3               # |lateral g| above this == in a corner (for sawing)
SAW_RATE_TIP = 1.5        # steering direction reversals per cornering second
MAX_CORNER_CALLOUTS = 3   # list at most this many under-driven corners


def _tip(area: str, observation: str, advice: str, confidence: str) -> dict:
    return {"area": area, "observation": observation, "advice": advice,
            "confidence": confidence}


def _braking_effort(report: dict) -> dict | None:
    grip = report.get("grip") or {}
    env, peak = grip.get("envelope_g"), grip.get("max_brake_g")
    if env is None or peak is None or env < MIN_ENVELOPE_G:
        return None
    if peak >= BRAKE_UTIL_TIP * env:
        return None
    return _tip(
        "braking",
        f"the car proves {env:.2f} g of grip in corners but you peak at only "
        f"{peak:.2f} g on the brakes ({100 * peak / env:.0f}% of the envelope)",
        "brake harder and later — initial pressure should reach near the grip "
        "limit, then bleed off toward the apex",
        "medium",
    )


def _coasting(samples: list[Sample]) -> dict | None:
    moving = [s for s in samples if s.speed_kmh > COAST_MIN_KMH]
    if not moving:
        return None
    coasting = [
        s for s in moving
        if (s.throttle or 0.0) < COAST_THROTTLE and (s.brake or 0.0) < COAST_BRAKE
    ]
    pct = 100.0 * len(coasting) / len(moving)
    if pct <= COAST_PCT_TIP:
        return None
    return _tip(
        "coasting",
        f"{pct:.0f}% of the moving lap is spent neither braking nor on throttle",
        "close the dead time: brake later into the corner, or get back to "
        "throttle earlier — the car should almost always be doing one or the other",
        "medium",
    )


def _underdriven_corners(report: dict) -> dict | None:
    grip = report.get("grip") or {}
    env = grip.get("envelope_g")
    if env is None or env < MIN_ENVELOPE_G:
        return None
    weak = [c for c in report.get("corners") or []
            if c.get("peak_lat_g", 0.0) < CORNER_UTIL_TIP * env]
    if not weak:
        return None
    weak.sort(key=lambda c: c["peak_lat_g"])
    spots = "; ".join(
        f"{c['direction']} at {c['dist_m']:.0f} m ({c['peak_lat_g']:.2f} g, "
        f"min {c['v_min_kmh']:.0f} km/h)"
        for c in weak[:MAX_CORNER_CALLOUTS])
    return _tip(
        "corner speed",
        f"{len(weak)} corner(s) peak well under the car's {env:.2f} g envelope: {spots}",
        "these corners have grip left on the table — carry more entry speed or "
        "get to the apex harder (distance markers are from the lap start)",
        "medium",
    )


def _steering_sawing(samples: list[Sample]) -> dict | None:
    in_corner = [(s.t, s.steering) for s in samples
                 if abs(s.gy) > SAW_G and s.steering is not None]
    if len(in_corner) < 10:
        return None
    duration = in_corner[-1][0] - in_corner[0][0]
    if duration <= 1.0:
        return None
    reversals = 0
    prev_sign = 0
    prev_val = in_corner[0][1]
    for _, val in in_corner[1:]:
        d = val - prev_val
        prev_val = val
        if abs(d) < 0.01:
            continue
        sign = 1 if d > 0 else -1
        if prev_sign and sign != prev_sign:
            reversals += 1
        prev_sign = sign
    rate = reversals / duration
    if rate <= SAW_RATE_TIP:
        return None
    return _tip(
        "steering",
        f"{rate:.1f} steering reversals per cornering second — sawing at the wheel",
        "commit to one steering input per corner and let the car take a set; "
        "if it genuinely won't hold a line, that's a setup problem — ask the "
        "race engineer about it",
        "low",  # normalized steering channel, not road-wheel degrees
    )


def _brake_release(report: dict) -> dict | None:
    braking = report.get("braking") or {}
    if not braking.get("unstable"):
        return None
    return _tip(
        "braking stability",
        f"yaw builds to {braking.get('straightline_yaw_instability')} rad/s while "
        "braking with the wheel straight",
        "squeeze the initial application instead of stabbing, and release "
        "progressively; if it stays twitchy it's the car (brake bias / rear "
        "damping) — hand it to the race engineer",
        "medium",
    )


def _throttle_commit(samples: list[Sample], report: dict) -> dict | None:
    if not report.get("valid"):
        return None
    with_throttle = [s for s in samples if s.throttle is not None]
    if not with_throttle:
        return None
    committed = sum(1 for s in with_throttle if (s.throttle or 0.0) >= FULL_THROTTLE)
    pct = 100.0 * committed / len(with_throttle)
    if pct >= 15.0:
        return None
    return _tip(
        "throttle",
        f"only {pct:.0f}% of the lap at full throttle",
        "if the track has straights, you may be short-shifting or feathering — "
        "commit to full throttle sooner off the corner (ignore this on a "
        "genuinely tight circuit)",
        "low",
    )


def coach_tips(samples: list[Sample], report: dict) -> list[dict]:
    """All driver-side tips for one lap, strongest evidence first."""
    tips = [
        _braking_effort(report),
        _brake_release(report),
        _coasting(samples),
        _underdriven_corners(report),
        _steering_sawing(samples),
        _throttle_commit(samples, report),
    ]
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted((t for t in tips if t), key=lambda t: order.get(t["confidence"], 3))


def coach(samples: list[Sample], report: dict) -> dict:
    """Driver-technique read of one lap: tips + a one-line headline."""
    if not (isinstance(report, dict) and report.get("ok")):
        return {"ok": False, "error": "lap did not analyze cleanly — nothing to coach"}
    tips = coach_tips(samples, report)
    if not report.get("valid"):
        reasons = ", ".join((report.get("validity") or {}).get("reasons") or [])
        headline = (f"lap is not clean ({reasons}) — drive a full uninterrupted lap "
                    "for coaching you can trust; tips below are provisional")
    elif not tips:
        headline = "clean lap, nothing to pick at — the next tenth is in the setup, not the driver"
    else:
        headline = f"{len(tips)} thing(s) to work on — start with {tips[0]['area']}"
    return {
        "ok": True,
        "headline": headline,
        "tips": tips,
        "valid_lap": bool(report.get("valid")),
        "note": ("driver-side advice only; for car-side changes describe the feel "
                 "to race_engineer"),
    }
