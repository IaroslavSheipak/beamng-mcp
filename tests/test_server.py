"""Server tool-layer tests — offline only. Covers the two tool surfaces (core
by default, full behind BEAMNG_FULL_SURFACE), tool registration parity, and the
MCP-boundary envelope contract (never raise)."""

import asyncio

from beamng_mcp.app import App
from beamng_mcp.server import create_server

#: The product contract for the DEFAULT surface: the whole engineer loop —
#: diagnose -> connect -> time -> analyze/coach -> plan -> apply -> compare ->
#: save — with no dead ends, and nothing else.
CORE_TOOLS = {
    "doctor", "connect", "disconnect", "reconnect", "status",
    "current_vehicles", "telemetry",
    "set_start_line", "clear_gates",
    "start_lap_session", "lap_session_status", "last_lap", "stop_lap_session",
    "analyze_lap", "compare_laps", "lap_coach",
    "race_engineer", "get_tuning_full", "apply_setup", "set_tire_pressure",
    "save_config",
}

POWER_TOOLS = {
    "list_vehicle_models", "list_configs", "spawn", "get_config", "set_config",
    "set_control", "run_test", "read_pc", "write_pc", "outgauge_telemetry",
    "start_logging", "stop_logging", "summarize_drive", "vehicle_lua",
    "start_lap", "lap_status", "stop_lap", "start_time_trial",
    "time_trial_status", "stop_time_trial", "set_tuning", "wheel_telemetry",
    "set_traction_control", "list_parts", "swap_parts", "car_mass",
}

PROMPTS = {"first_time_setup", "pit_wall_session", "track_day_debrief"}


def _server(full: bool = False):
    app = App()
    return app, create_server(app, full=full)


def test_default_surface_is_exactly_the_core():
    _, mcp = _server()
    names = {t.name for t in mcp._tool_manager.list_tools()}
    assert names == CORE_TOOLS


def test_full_surface_is_core_plus_power():
    _, mcp = _server(full=True)
    names = {t.name for t in mcp._tool_manager.list_tools()}
    assert names == CORE_TOOLS | POWER_TOOLS
    assert len(names) == 47


def test_prompts_registered_on_both_surfaces():
    for full in (False, True):
        _, mcp = _server(full=full)
        names = {p.name for p in mcp._prompt_manager.list_prompts()}
        assert PROMPTS <= names


def test_prompts_reference_only_core_tools():
    """The guided workflows must never point a core-surface client at a tool
    it doesn't have."""
    _, mcp = _server()
    for prompt in mcp._prompt_manager.list_prompts():
        messages = asyncio.run(mcp._prompt_manager.render_prompt(prompt.name))
        content = "".join(m.content.text for m in messages)
        for name in POWER_TOOLS:
            assert name + "(" not in content, f"{prompt.name} references power tool {name}"


def test_offline_tools_never_raise_across_the_boundary():
    _, mcp = _server(full=True)
    tm = mcp._tool_manager
    for name in ("current_vehicles", "get_config", "race_engineer", "start_lap", "wheel_telemetry"):
        args = {"feedback": "test"} if name == "race_engineer" else {}
        out = asyncio.run(tm.call_tool(name, args))
        # The MCP boundary contract this layer exists for: every call -- success
        # or service-layer failure alike -- comes back as an {"ok": ...} dict,
        # never a raised exception leaking out as an opaque isError result.
        assert isinstance(out, dict) and "ok" in out
        assert out["ok"] is False  # nothing is connected in this test


def test_compare_laps_bad_paths_is_a_clean_envelope():
    _, mcp = _server()
    out = asyncio.run(mcp._tool_manager.call_tool(
        "compare_laps", {"path_a": "/nope/a.csv", "path_b": "/nope/b.csv"}))
    assert out["ok"] is False and "error" in out


def test_lap_status_is_offline_safe():
    _, mcp = _server(full=True)
    out = asyncio.run(mcp._tool_manager.call_tool("lap_status", {}))
    assert out["ok"] is True
    assert out["logging"] is False


def test_status_offline_envelope():
    _, mcp = _server()
    out = asyncio.run(mcp._tool_manager.call_tool("status", {}))
    assert out["ok"] is True
    assert out["connected"] is False


def test_fresh_app_instances_are_independent():
    app_a, mcp_a = _server()
    app_b, mcp_b = _server()
    assert app_a is not app_b
    assert mcp_a is not mcp_b
