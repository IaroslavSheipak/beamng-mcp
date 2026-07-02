"""raceline: the pure segment builder — decimation, bucketing, run-joining,
and the delta-gradient coloring — without a game."""

import pytest

from beamng_mcp.analysis.model import sample_from_row
from beamng_mcp.errors import BeamNGError
from beamng_mcp.sim import raceline


def _lap(n=200, dt=0.1, speeds=None):
    out = []
    for i in range(n):
        v = speeds(i) if speeds else 16.6
        out.append(sample_from_row({
            "t": i * dt, "dist": 1000.0 * i / (n - 1), "speed": v,
            "gy": 0.0, "gx": 0.0, "heading": 0.0,
            "posx": float(i), "posy": 0.0, "posz": 100.0,
        }))
    return out


def test_speed_coloring_follows_gt7_convention():
    # GT7 driving line: RED = slow (braking zones), blue = fast
    lap = _lap(speeds=lambda i: 5.0 if i < 100 else 40.0)
    segs = raceline.build_segments(lap, color_by="speed")
    assert len(segs) >= 2
    first_rgba, last_rgba = segs[0][1], segs[-1][1]
    assert first_rgba[0] > first_rgba[2]   # slow half: red channel dominates
    assert last_rgba[2] > last_rgba[0]     # fast half: blue channel dominates


def test_runs_join_consecutive_same_bucket_points():
    segs = raceline.build_segments(_lap(), color_by="speed")  # constant speed
    assert len(segs) == 1                  # one bucket -> one polyline
    assert len(segs[0][0]) <= raceline.MAX_POINTS + 1


def test_decimation_caps_points():
    segs = raceline.build_segments(_lap(n=5000), color_by="speed")
    assert sum(len(p) for p, _ in segs) <= raceline.MAX_POINTS + len(segs)


def test_segments_are_continuous():
    lap = _lap(speeds=lambda i: 5.0 + (i % 40))
    segs = raceline.build_segments(lap, color_by="speed")
    for (pts_a, _), (pts_b, _) in zip(segs, segs[1:]):
        assert pts_a[-1] == pts_b[0]  # each run starts where the last ended


def test_delta_coloring_needs_a_reference():
    with pytest.raises(BeamNGError, match="reference"):
        raceline.build_segments(_lap(), color_by="delta", ref=None)


def test_delta_coloring_marks_the_losing_sector_red():
    ref = _lap(dt=0.10)
    # candidate loses time only in the middle third (distance frozen in time:
    # same path, but t grows faster there)
    cand = []
    t = 0.0
    for i in range(200):
        t += 0.10 if not (66 <= i < 133) else 0.16
        cand.append(sample_from_row({
            "t": t, "dist": 1000.0 * i / 199, "speed": 16.6,
            "gy": 0.0, "gx": 0.0, "heading": 0.0,
            "posx": float(i), "posy": 0.0, "posz": 100.0,
        }))
    segs = raceline.build_segments(cand, color_by="delta", ref=ref)
    reds = [rgba for _, rgba in segs if rgba[0] > rgba[2] + 0.2]
    assert reds, "the slow middle sector must show red (losing time)"


def test_no_position_trace_is_a_clean_error():
    lap = [sample_from_row({"t": i * 0.1, "dist": i, "speed": 10.0})
           for i in range(50)]
    with pytest.raises(BeamNGError, match="position"):
        raceline.build_segments(lap, color_by="speed")


def test_diverging_rgba_endpoints_and_midpoint():
    assert raceline.diverging_rgba(-1.0)[:3] == pytest.approx(raceline._BLUE)
    assert raceline.diverging_rgba(1.0)[:3] == pytest.approx(raceline._RED)
    assert raceline.diverging_rgba(0.0)[:3] == pytest.approx(raceline._NEUTRAL)
