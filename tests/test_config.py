import os

from beamng_mcp.config import DEFAULT_PORT, Settings


def test_defaults():
    s = Settings.from_env()
    assert s.port == DEFAULT_PORT
    assert s.host == "127.0.0.1"
    assert s.user_vehicles.endswith("vehicles")
    # The -userpath is the PARENT of the version folder, not the version folder.
    assert s.userpath_root == os.path.dirname(s.user_folder)


def test_env_override(monkeypatch):
    monkeypatch.setenv("BEAMNG_PORT", "30000")
    monkeypatch.setenv("BEAMNG_HOST", "10.0.0.5")
    monkeypatch.setenv("BEAMNG_USER", r"C:\games\BeamNG\current")
    s = Settings.from_env()
    assert s.port == 30000
    assert s.host == "10.0.0.5"
    assert s.user_vehicles == os.path.join(r"C:\games\BeamNG\current", "vehicles")


def test_settings_is_frozen():
    s = Settings.from_env()
    try:
        s.port = 1  # type: ignore[misc]
    except Exception as exc:  # FrozenInstanceError
        assert "FrozenInstanceError" in type(exc).__name__ or "cannot assign" in str(exc)
    else:
        raise AssertionError("Settings should be immutable")
