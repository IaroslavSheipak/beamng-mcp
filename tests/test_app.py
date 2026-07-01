"""App wiring tests — offline only (no live BeamNGpy connection)."""

from types import SimpleNamespace

import pytest

from beamng_mcp.app import App
from beamng_mcp.errors import BeamNGError, NotConnected


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
