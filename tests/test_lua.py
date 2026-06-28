import pytest

from beamng_mcp.errors import LuaError
from beamng_mcp.sim import lua


def test_lua_json_valid_string():
    assert lua.lua_json('{"ok": true, "count": 2}') == {"ok": True, "count": 2}


def test_lua_json_passthrough_dict():
    assert lua.lua_json({"ok": True}) == {"ok": True}


def test_lua_json_none_raises():
    # The exact gap that crashed v1 wheel_telemetry: a nil/None Lua result.
    with pytest.raises(LuaError):
        lua.lua_json(None)


def test_lua_json_non_json_raises():
    with pytest.raises(LuaError):
        lua.lua_json("not json at all")


def test_lua_json_non_object_raises():
    with pytest.raises(LuaError):
        lua.lua_json("[1, 2, 3]")  # valid JSON, but an array not an object


def test_traction_control_lua_toggles():
    on = lua.traction_control_lua(True)
    off = lua.traction_control_lua(False)
    assert "isEnabled=true" in on and "toggled" in on
    assert "isEnabled=false" in off


def test_chunks_reach_known_internals():
    assert "v.data.variables" in lua.FULL_VARS_LUA
    assert "wheels.wheels" in lua.WHEELS_LUA
