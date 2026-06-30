import pytest

from beamng_mcp.errors import BeamNGError
from beamng_mcp.sim.context import Simulator
from beamng_mcp.timing.statemachine import LapTimer, _motion_fields, fmt_time


def _timer(tmp_path):
    return LapTimer(Simulator(), str(tmp_path))


def test_fmt_time():
    assert fmt_time(65.891) == "1:05.891"
    assert fmt_time(5.0) == "0:05.000"
    assert fmt_time(125.5) == "2:05.500"


def test_busy_idle(tmp_path):
    assert _timer(tmp_path).busy() is None


def test_busy_reflects_modes(tmp_path):
    t = _timer(tmp_path)
    t._tt = {"state": "running"}
    assert t.busy() == "time_trial"
    t._tt = {"state": "idle"}
    t._sess = {"state": "running"}
    assert t.busy() == "lap_session"


def test_busy_reflects_recorder(tmp_path):
    t = _timer(tmp_path)
    t.recorder.start(lambda: {"speed": 0.0}, hz=20.0)
    try:
        assert t.busy() == "lap"
    finally:
        t.recorder.stop()


def test_start_lap_rejected_when_busy(tmp_path):
    # The whole point of the redesign: a second mode is refused BEFORE any game
    # call, so the three modes can never share the recorder.
    t = _timer(tmp_path)
    t._sess = {"state": "running"}
    with pytest.raises(BeamNGError):
        t.start_lap()


def test_start_time_trial_rejected_when_lap_running(tmp_path):
    t = _timer(tmp_path)
    t.recorder.start(lambda: {"speed": 0.0}, hz=20.0)
    try:
        with pytest.raises(BeamNGError):
            t.start_time_trial()
    finally:
        t.recorder.stop()


def test_last_lap_empty_raises(tmp_path):
    with pytest.raises(BeamNGError):
        _timer(tmp_path).last_lap()


def test_status_idle_shapes(tmp_path):
    t = _timer(tmp_path)
    assert t.time_trial_status()["state"] == "idle"
    s = t.lap_session_status()
    assert s["state"] == "idle" and s["count"] == 0 and s["best"] is None


def test_motion_fields_none_listener_is_empty():
    assert _motion_fields(None) == {}


def test_motion_fields_no_fresh_packet_is_empty():
    class _NoPacket:
        def latest(self):
            return None

    assert _motion_fields(_NoPacket()) == {}


def test_motion_fields_extracts_yaw_rate_and_accel():
    class _FakeListener:
        def latest(self):
            return {"acc": (1.0, 2.0, 3.0), "ang_vel": (0.1, 0.2, 0.3)}

    out = _motion_fields(_FakeListener())
    assert out == {"ms_yaw_rate": 0.3, "ms_ax": 1.0, "ms_ay": 2.0, "ms_az": 3.0}


def test_motion_field_names_are_recorder_columns():
    # The recorder must have a CSV column for every key _motion_fields can emit.
    from beamng_mcp.timing.recorder import RICH_FIELDS

    class _FakeListener:
        def latest(self):
            return {"acc": (0.0, 0.0, 0.0), "ang_vel": (0.0, 0.0, 0.0)}

    assert set(_motion_fields(_FakeListener())) <= set(RICH_FIELDS)
