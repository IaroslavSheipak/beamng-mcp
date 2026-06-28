import math

from beamng_mcp.analysis import balance
from beamng_mcp.analysis.model import sample_from_row


def mk(**kw):
    return sample_from_row(kw)


def test_yaw_rate_from_heading():
    s = [mk(t=0, heading=0.0), mk(t=0.1, heading=0.1), mk(t=0.2, heading=0.2)]
    yr = balance.yaw_rates(s)
    assert round(yr[0], 3) == 1.0 and round(yr[1], 3) == 1.0
    assert yr[2] is None  # last sample has no interval


def test_slip_angle_zero_when_gripping():
    # Moving +x while pointing +x -> slip ~ 0.
    s = [mk(posx=0.0, posy=0.0, heading=0.0), mk(posx=1.0, posy=0.0, heading=0.0)]
    assert abs(balance.slip_angles(s)[0]) < 1e-6


def test_slip_angle_detects_slide():
    # Travelling +x but pointing 10 deg off -> slip = -10 deg.
    s = [mk(posx=0.0, posy=0.0, heading=math.radians(10)), mk(posx=1.0, posy=0.0, heading=math.radians(10))]
    assert round(balance.slip_angles(s)[0], 1) == -10.0


def _self_consistent_lap():
    """A car that exactly obeys yaw = k*steer*v/L (k=0.5): genuinely NEUTRAL.
    The fixed-gain v1 index would still report ~+1.0; the self-calibrated one ~0."""
    k, v, L, dt = 0.5, 20.0, 2.6, 0.1
    heading, t = 0.0, 0.0
    samples = []
    for count, yawt, gy in ((60, 0.1, 0.1), (40, 0.5, 0.5)):  # low-g calib, then corners
        steer = yawt * L / (k * v)
        for _ in range(count):
            samples.append(mk(t=t, heading=heading, gy=gy, steering=steer, speed=v))
            heading += yawt * dt
            t += dt
    samples.append(mk(t=t, heading=heading, gy=0.0, steering=0.0, speed=v))
    return samples


def test_neutral_car_is_not_pinned_at_one():
    b = balance.balance(_self_consistent_lap())
    assert b["understeer_index"] is not None
    assert abs(b["understeer_index"]) < 0.1, b  # ~0, the v1 bug would give ~1.0
    assert b["tendency"] == "neutral"
    assert b["confidence"] in ("medium", "low")


def test_index_is_null_without_calibration_data():
    # All-cornering, no low-g samples -> cannot self-calibrate -> honest null.
    k, v, L, dt = 0.5, 20.0, 2.6, 0.1
    heading, t = 0.0, 0.0
    samples = []
    steer = 0.5 * L / (k * v)
    for _ in range(60):
        samples.append(mk(t=t, heading=heading, gy=0.8, steering=steer, speed=v))
        heading += 0.5 * dt
        t += dt
    b = balance.balance(samples)
    assert b["understeer_index"] is None
    assert b["confidence"] == "none"
    assert b["tendency"] == "unknown"
