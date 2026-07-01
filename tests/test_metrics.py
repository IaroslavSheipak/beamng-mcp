from beamng_mcp.analysis import braking, corners, grip, ride
from beamng_mcp.analysis.balance import yaw_rates
from beamng_mcp.analysis.model import sample_from_row


def mk(**kw):
    return sample_from_row(kw)


def test_grip_envelope_is_percentile_not_max():
    cleaned = [mk(gy=1.0, gx=0.1) for _ in range(50)] + [mk(gy=1.5, gx=0.1)]
    g = grip.grip(cleaned)
    assert g["max_lat_g"] == 1.5
    # the lone high sample doesn't set the p98 envelope
    assert g["envelope_g"] < 1.3


def test_grip_empty():
    g = grip.grip([])
    assert g["max_lat_g"] is None


def test_braking_counts_events_and_peak():
    s = [mk(brake=0.0, gx=0.1), mk(brake=0.5, gx=-0.8), mk(brake=0.5, gx=-1.2),
         mk(brake=0.0, gx=0.1), mk(brake=0.6, gx=-0.5)]
    b = braking.braking(s, yaw_rates(s))
    assert b["events"] == 2           # two separate brake applications
    assert b["peak_decel_g"] == 1.2


def test_ride_bottoming():
    s = [mk(gz=1.0) for _ in range(20)] + [mk(gz=2.0), mk(gz=2.1)]  # two spikes
    r = ride.ride(s)
    assert r["bottoming_events"] == 2
    assert r["settle_quality"] is not None


def test_corners_segments_a_turn():
    s = ([mk(gy=0.0, speed=30, dist=0)] * 3
         + [mk(gy=0.8, speed=20, dist=d) for d in (10, 11, 12, 13)]
         + [mk(gy=0.0, speed=30, dist=20)] * 3)
    c = corners.corners(s)
    assert len(c) == 1
    assert c[0]["peak_lat_g"] == 0.8
    assert c[0]["v_min_kmh"] == 72.0  # 20 m/s
