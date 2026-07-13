"""App wiring tests — offline only (no live BeamNGpy connection)."""

import os
import time
from types import SimpleNamespace

import pytest

from beamng_mcp.app import App
from beamng_mcp.config import Settings
from beamng_mcp.errors import BeamNGError, NotConnected
from beamng_mcp.timing.line import StartLine


def test_construction_has_no_side_effects():
    app = App()
    assert app.sim.is_connected() is False
    assert app.motion.running is False
    assert app.timer._motion is app.motion
    assert app.timer.recorder._logs_dir == app.settings.logs_dir
    assert app.drivelog._logs_dir == app.settings.logs_dir


def test_connect_starts_motion_disconnect_stops_it(monkeypatch):
    app = App()

    def fake_connect(**kwargs):
        # is_connected() becomes True without a real game; close() must be a
        # no-op so sim.disconnect() can run its normal teardown path below.
        app.sim.bng = SimpleNamespace(close=lambda: None)
        return {"connected": True}

    monkeypatch.setattr(app.sim, "connect", fake_connect)
    app.connect()
    assert app.motion.running is True

    app.sim.disconnect()
    assert app.motion.running is False


def test_race_engineer_requires_connection():
    app = App()
    with pytest.raises(NotConnected):
        app.race_engineer("understeer on entry")


def test_apply_setup_requires_something_to_apply():
    app = App()
    with pytest.raises(BeamNGError):
        app.apply_setup()
    with pytest.raises(BeamNGError):
        app.apply_setup(plan=[], vars={})


# -- apply_setup vs an open lap (the respawn corrupts it) ---------------------
class _FakeVehicle:
    """Enough of a beamngpy Vehicle for the timer: scripted cached state."""

    def __init__(self):
        self.state = {"pos": [-50.0, 0.0, 0.0], "vel": [30.0, 0.0, 0.0],
                      "dir": [1.0, 0.0, 0.0]}
        self.sensors = {"electrics": {}, "gforces": {}}

    def is_connected(self):
        return True

    def poll_sensors(self):
        pass


def _wired_app(tmp_path):
    """App over a fake game, ready to lap: the start line is the x=0 plane."""
    app = App(Settings(game_home="", user_folder="", userpath_root="",
                       host="127.0.0.1", port=25252, logs_dir=str(tmp_path)))
    app.sim.bng = SimpleNamespace(
        vehicles=SimpleNamespace(get_player_vehicle_id=lambda: {"vid": "car"}))
    veh = _FakeVehicle()
    app.sim.vehicles["car"] = veh
    app.timer.line = StartLine(pos=[0.0, 0.0, 0.0], heading=[1.0, 0.0, 0.0])
    return app, veh


def _fake_set_vars(applied):
    def fake(sim, vmap, vid=None):
        applied.append(dict(vmap))
        return {"vid": "car", "applied": vmap, "respawned": True,
                "note": "applied via set_part_config — car respawned."}
    return fake


def _wait(pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def _cross(timer, veh, timeout=6.0):
    """Script one line crossing: arm far behind the x=0 plane, then step past it
    (retried until a worker tick has seen both positions)."""
    before = timer._sess.get("t_cross")
    deadline = time.time() + timeout
    while time.time() < deadline:
        veh.state["pos"] = [-50.0, 0.0, 0.0]
        time.sleep(0.3)
        veh.state["pos"] = [5.0, 0.0, 0.0]
        if _wait(lambda: timer._sess.get("t_cross") not in (None, before), timeout=0.5):
            return True
    return False


def test_apply_setup_mid_session_discards_partial_and_rearms(tmp_path, monkeypatch):
    app, veh = _wired_app(tmp_path)
    applied = []
    monkeypatch.setattr("beamng_mcp.app.tuning.set_tuning_vars", _fake_set_vars(applied))
    app.timer.start_lap_session(hz=50.0)
    try:
        assert _cross(app.timer, veh)  # a lap is now open
        assert _wait(lambda: app.timer.recorder.running)
        open_csv = app.timer.recorder.path
        out = app.apply_setup(vars={"$brakebias": 0.6})
        assert applied == [{"$brakebias": 0.6}]  # abort happened, apply still ran
        assert out["session"] == "re-armed" and out["discarded_partial"] is True
        assert "discarded" in out["note"]
        assert not os.path.exists(open_csv)  # the partial never survives as lap_*.csv
        # the pit board wraps this same timer: its session must still be running
        st = app.timer.lap_session_status()
        assert st["state"] == "running" and st["count"] == 0
    finally:
        app.timer.stop_lap_session()


def test_apply_setup_while_idle_reports_idle(tmp_path, monkeypatch):
    app, _ = _wired_app(tmp_path)
    monkeypatch.setattr("beamng_mcp.app.tuning.set_tuning_vars", _fake_set_vars([]))
    out = app.apply_setup(vars={"$brakebias": 0.6})
    assert out["session"] == "idle" and out["discarded_partial"] is False
    assert "discarded" not in out["note"]
