"""Geometry tests for the interpolated plane-crossing detector — ported from the
v1 fix's live-validated unit tests."""

import math

from beamng_mcp.timing.line import StartLine, gate_endpoints, line_cross

LINE = StartLine(pos=[0.0, 0.0, 0.0], heading=[1.0, 0.0, 0.0])  # at origin, facing +x


def near(a, b, tol=1e-6):
    return a is not None and abs(a - b) < tol


def test_midpoint_crossing_interpolates():
    assert near(line_cross((0.0, [-1.0, 0.0, 0.0]), (0.1, [1.0, 0.0, 0.0]), LINE), 0.05)


def test_distance_weighted_interpolation():
    # from -0.2 to +0.8 -> crosses at 20% of the interval
    assert near(line_cross((0.0, [-0.2, 0.0, 0.0]), (0.1, [0.8, 0.0, 0.0]), LINE), 0.02)


def test_wide_of_gate_ignored():
    # crosses the plane but 10 m to the side (> 6 m half-width)
    assert line_cross((0.0, [-1.0, 10.0, 0.0]), (0.1, [1.0, 10.0, 0.0]), LINE) is None


def test_wrong_direction_ignored():
    # ahead -> behind is not a lap close
    assert line_cross((0.0, [1.0, 0.0, 0.0]), (0.1, [-1.0, 0.0, 0.0]), LINE) is None


def test_no_crossing_returns_none():
    assert line_cross((0.0, [-5.0, 0.0, 0.0]), (0.1, [-3.0, 0.0, 0.0]), LINE) is None


def test_old_proximity_false_positive_is_rejected():
    # The v1 bug: a parallel straight 8 m to the side fired (<10 m sphere). Now: None.
    assert line_cross((0.0, [-1.0, 8.0, 0.0]), (0.1, [1.0, 8.0, 0.0]), LINE) is None


def test_diagonal_gate():
    diag = StartLine(pos=[10.0, 10.0, 0.0], heading=[0.6, 0.8, 0.0])
    behind = [10.0 - 0.6, 10.0 - 0.8, 0.0]
    ahead = [10.0 + 0.6, 10.0 + 0.8, 0.0]
    assert near(line_cross((2.0, behind), (2.1, ahead), diag), 2.05)


def test_none_samples():
    assert line_cross(None, (0.0, [1.0, 0.0, 0.0]), LINE) is None
    assert line_cross((0.0, [1.0, 0.0, 0.0]), None, LINE) is None


def test_gate_endpoints_perpendicular_and_centered():
    a, b = gate_endpoints(LINE, half=6.0)
    # heading +x -> gate runs along y, endpoints at (0, +-6)
    assert near(a[1], 6.0) and near(b[1], -6.0)
    assert near(a[0], 0.0) and near(b[0], 0.0)
    # midpoint is the line position
    assert near((a[0] + b[0]) / 2, 0.0) and near((a[1] + b[1]) / 2, 0.0)
    # full width == 2 * half
    assert near(math.dist(a, b), 12.0)
