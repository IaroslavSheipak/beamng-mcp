import pytest

from beamng_mcp.errors import NotConnected
from beamng_mcp.sim import vehicle
from beamng_mcp.sim.context import Simulator


def test_evict_removes_handle_and_sensors():
    sim = Simulator()
    sim.vehicles["ego"] = object()
    sim.sensors["ego"] = {"electrics": {}}
    vehicle.evict(sim, "ego")
    assert "ego" not in sim.vehicles
    assert "ego" not in sim.sensors


def test_evict_none_is_noop():
    sim = Simulator()
    sim.vehicles["ego"] = object()
    vehicle.evict(sim, None)
    assert "ego" in sim.vehicles  # untouched


def test_require_connected_raises_when_offline():
    sim = Simulator()
    assert sim.is_connected() is False
    with pytest.raises(NotConnected):
        sim.require_connected()


def test_status_offline_shape():
    sim = Simulator()
    st = sim.status()
    assert st["connected"] is False
    assert st["port"] == sim.settings.port
    assert st["vehicles"] == []
