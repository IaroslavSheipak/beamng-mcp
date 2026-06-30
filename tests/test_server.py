"""Server tool-layer tests — offline only, mirrors v1 smoke_test.py's tool-
registration checks plus the MCP-boundary envelope contract (never raise)."""

import asyncio

from beamng_mcp.app import App
from beamng_mcp.server import create_server


def _server():
    app = App()
    return app, create_server(app)


def test_registers_full_v1_parity_tool_surface():
    _, mcp = _server()
    names = {t.name for t in mcp._tool_manager.list_tools()}
    assert len(names) >= 40
    assert {
        "connect", "disconnect", "reconnect", "status", "current_vehicles",
        "list_vehicle_models", "list_configs", "spawn", "telemetry", "get_config",
        "set_config", "set_control", "run_test", "read_pc", "write_pc",
        "outgauge_telemetry", "start_logging", "stop_logging", "summarize_drive",
        "vehicle_lua", "start_lap", "stop_lap", "analyze_lap", "race_engineer",
        "get_tuning_full", "set_tuning", "apply_setup", "set_tire_pressure",
        "wheel_telemetry", "set_start_line", "start_time_trial", "time_trial_status",
        "stop_time_trial", "start_lap_session", "lap_session_status", "last_lap",
        "stop_lap_session", "set_traction_control", "list_parts", "swap_parts",
        "save_config", "car_mass", "clear_gates",
    } <= names


def test_offline_tools_never_raise_across_the_boundary():
    _, mcp = _server()
    tm = mcp._tool_manager
    for name in ("current_vehicles", "get_config", "race_engineer", "start_lap", "wheel_telemetry"):
        args = {"feedback": "test"} if name == "race_engineer" else {}
        out = asyncio.run(tm.call_tool(name, args))
        # The MCP boundary contract this layer exists for: every call -- success
        # or service-layer failure alike -- comes back as an {"ok": ...} dict,
        # never a raised exception leaking out as an opaque isError result.
        assert isinstance(out, dict) and "ok" in out
        assert out["ok"] is False  # nothing is connected in this test


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
