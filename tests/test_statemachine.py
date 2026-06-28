import pytest

from beamng_mcp.errors import BeamNGError
from beamng_mcp.sim.context import Simulator
from beamng_mcp.timing.statemachine import LapTimer, fmt_time


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
