"""scenario._resolve_config — the only offline-testable piece of spawn/run_test
(the rest needs a live BeamNGpy connection, like the rest of sim/)."""

from types import SimpleNamespace

import pytest

from beamng_mcp.sim import pc_config
from beamng_mcp.sim.scenario import _resolve_config


def test_resolve_config_none_passthrough():
    assert _resolve_config("bx", None) is None


def test_resolve_config_missing_raises():
    with pytest.raises(ValueError):
        _resolve_config("bx", "no_such_config")


def test_resolve_config_resolves_confined_path(tmp_path, monkeypatch):
    # _resolve_config (like v1) always reads against the configured user
    # vehicles dir, so swap the module's SETTINGS singleton for the duration.
    monkeypatch.setattr(pc_config, "SETTINGS", SimpleNamespace(user_vehicles=str(tmp_path)))
    pc_config.write_pc("bx", "race", {"format": 2, "model": "bx", "parts": {}, "vars": {}})
    path = _resolve_config("bx", "race")
    assert path is not None and path.endswith("race.pc")
