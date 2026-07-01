"""coach: driver-side tips must be evidence-gated — a committed clean lap earns
silence, and each weakness trips exactly its own detector."""

from beamng_mcp.analysis import coach, report
from beamng_mcp.analysis.model import sample_from_row


def _lap(n=200, dist=1200.0, speed=20.0, corner_g=1.0, brake_g=0.9,
         throttle=1.0, coast=False, saw=False, stopped_frac=0.0):
    """Blocks of 10 corner / 10 straight samples; braking opens each straight.
    The default lap is COMMITTED: near-envelope braking, full throttle, one
    steady steering input — it must earn zero tips."""
    n_stop = int(n * stopped_frac)
    out = []
    for i in range(n):
        in_corner = (i // 10) % 2 == 0
        braking = (not in_corner) and (i % 10) < 3  # first 3 samples of each straight
        gy = corner_g if in_corner else 0.0
        gx = -brake_g if braking else 0.15
        thr = 0.0 if (braking or coast) else throttle
        brk = 0.9 if braking else 0.0
        if saw and in_corner:
            steer = 0.5 if i % 2 == 0 else -0.5
        else:
            steer = 0.4 if in_corner else 0.0
        out.append(sample_from_row({
            "t": i * 0.1, "dist": dist * i / (n - 1),
            "speed": 0.0 if i < n_stop else speed,
            "gy": gy, "gx": gx, "heading": 0.0, "posx": float(i), "posy": 0.0,
            "throttle": thr, "brake": brk, "steering": steer,
        }))
    return out


def _coach(samples):
    return coach.coach(samples, report.analyze_samples(samples))


def test_committed_clean_lap_earns_no_tips():
    out = _coach(_lap())
    assert out["ok"] is True
    assert out["tips"] == []
    assert "nothing to pick at" in out["headline"]


def test_weak_braking_draws_the_braking_tip():
    out = _coach(_lap(brake_g=0.3))
    areas = [t["area"] for t in out["tips"]]
    assert "braking" in areas
    tip = next(t for t in out["tips"] if t["area"] == "braking")
    assert "brake harder" in tip["advice"]


def test_coasting_draws_the_coasting_tip():
    out = _coach(_lap(coast=True))
    assert any(t["area"] == "coasting" for t in out["tips"])


def test_sawing_at_the_wheel_is_low_confidence():
    out = _coach(_lap(saw=True))
    tip = next(t for t in out["tips"] if t["area"] == "steering")
    assert tip["confidence"] == "low"  # normalized channel, not road-wheel degrees


def test_underdriven_corners_are_called_out_by_marker():
    # one lap where half the corners are driven far under the proven envelope
    samples = _lap()
    weak = _lap(corner_g=0.4)
    mixed = samples[:100] + weak[100:]
    out = _coach(mixed)
    tip = next((t for t in out["tips"] if t["area"] == "corner speed"), None)
    assert tip is not None
    assert " m " in tip["observation"] or " m," in tip["observation"] or "m (" in tip["observation"]


def test_invalid_lap_headline_says_so():
    out = _coach(_lap(stopped_frac=0.15))
    assert out["ok"] is True
    assert out["valid_lap"] is False
    assert "not clean" in out["headline"]


def test_tips_sorted_strongest_confidence_first():
    out = _coach(_lap(brake_g=0.3, saw=True))
    confs = [t["confidence"] for t in out["tips"]]
    order = {"high": 0, "medium": 1, "low": 2}
    assert confs == sorted(confs, key=lambda c: order[c])
