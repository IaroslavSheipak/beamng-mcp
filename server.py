"""server.py — FastMCP (stdio) server for BeamNG.drive (Steam consumer build).

Scope: NO BeamNG.tech. Classic polled sensors only (Electrics/State/Damage/
Timer/GForces) plus license-free OutGauge UDP telemetry. Every tool returns a
JSON-serializable dict and never raises across the MCP boundary.

Run on Windows:
    C:\\Users\\Iaroslav\\beamng-mcp\\.venv\\Scripts\\python.exe server.py
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

import outgauge
import pc_config
from session import session

mcp = FastMCP("beamng-mcp")

# Known drivable stock models (fallback list for list_vehicle_models).
STOCK_MODELS = [
    "autobello", "ball", "barstow", "bastion", "bluebuck", "bolide", "burnside",
    "bx", "citybus", "covet", "etk800", "etki", "etkc", "fullsize", "hopper",
    "lansdale", "legran", "midsize", "midtruck", "miramar", "moonhawk", "nine",
    "pessima", "pickup", "roamer", "sbr", "scintilla", "semi", "sunburst",
    "vivace", "wendover", "wigeon",
]


# --- 1. connect (GAME, LIVE) -------------------------------------------------
@mcp.tool()
def connect(home: str | None = None, user: str | None = None,
            host: str = "127.0.0.1", port: int = 25252,
            launch: bool = False) -> dict:
    """ATTACH to the BeamNG.drive game the user is ALREADY running (default).

    Does NOT launch the game, load a scenario, or spawn anything — it just opens
    the -tcom socket so other tools can read/modify the player's current car. The
    user must have started the game with '-tcom -tport 25252' (e.g. Steam launch
    options). Disconnecting an attached session LEAVES THE GAME RUNNING. Pass
    launch=True only to have the server start (and own) its own instance.
    """
    try:
        return session.connect(home=home, user=user, host=host, port=port,
                               launch=launch)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc)}


# --- 2. disconnect (GAME, LIVE) ---------------------------------------------
@mcp.tool()
def disconnect() -> dict:
    """Close the BeamNGpy session and clear all session state."""
    try:
        return session.disconnect()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc)}


# --- 3. status (OFFLINE, VERIF) ---------------------------------------------
@mcp.tool()
def status() -> dict:
    """Report connection state, active scenario, spawned vehicles, and config."""
    try:
        return session.status()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc)}


# --- current_vehicles (GAME, ATTACH) ----------------------------------------
@mcp.tool()
def current_vehicles() -> dict:
    """List the vehicles already in the running game, flagging the car the user
    is driving. Read-only; spawns nothing. Call after connect() to find what to
    read/tune. Live-tested on the Steam build.
    """
    try:
        return session.current_vehicles()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc)}


# --- 4. list_vehicle_models (OFFLINE, VERIF) --------------------------------
@mcp.tool()
def list_vehicle_models() -> dict:
    """List drivable models from install content/vehicles + user vehicles."""
    try:
        install_models: set[str] = set()
        if os.path.isdir(pc_config.INSTALL_VEHICLES):
            for f in os.listdir(pc_config.INSTALL_VEHICLES):
                if f.endswith(".zip"):
                    install_models.add(f[:-4])
        user_models: set[str] = set()
        if os.path.isdir(pc_config.USER_VEHICLES):
            for d in os.listdir(pc_config.USER_VEHICLES):
                if os.path.isdir(os.path.join(pc_config.USER_VEHICLES, d)):
                    user_models.add(d)
        all_models = install_models | user_models | set(STOCK_MODELS)
        return {
            "ok": True,
            "models": sorted(all_models),
            "source_counts": {
                "install": len(install_models),
                "user": len(user_models),
                "stock_fallback": len(STOCK_MODELS),
            },
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc)}


# --- 5. list_configs (OFFLINE, VERIF) ---------------------------------------
@mcp.tool()
def list_configs(model: str | None = None) -> dict:
    """List available .pc configs in the user vehicles folder."""
    try:
        return {"ok": True, "configs": pc_config.list_pc(model)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc)}


# --- 6. spawn (GAME, LIVE) --------------------------------------------------
@mcp.tool()
def spawn(model: str, config: str | None = None, vid: str = "ego",
          pos: list[float] | None = None, rot_quat: list[float] | None = None,
          level: str = "gridmap_v2") -> dict:
    """ACTIVE MODE (takes over the session): spawn a NEW vehicle, creating/
    loading a scenario if none is active. NOT used for normal 'change car config'
    — that operates on your current car via get_config/set_config. Use spawn only
    if you explicitly want a fresh test car on a test level.
    """
    try:
        return session.spawn(
            model=model,
            config=config,
            vid=vid,
            pos=pos if pos is not None else [0, 0, 0],
            rot_quat=rot_quat if rot_quat is not None else [0, 0, 0, 1],
            level=level,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc)}


# --- 7. telemetry (GAME, LIVE) ----------------------------------------------
@mcp.tool()
def telemetry(vid: str | None = None) -> dict:
    """Poll live telemetry of the car you're CURRENTLY driving (vid=None): 126
    Electrics channels (rpm, wheelspeed, gear, throttle, brake, fuel, oil/water
    temps, boost, accelerations, per-wheel brake temps...) plus Damage/GForces
    and State kinematics. Verified live on the Steam build.
    """
    try:
        return session.telemetry(vid=vid)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc)}


# --- 8. get_tuning (GAME, LIVE) ---------------------------------------------
@mcp.tool()
def get_config(vid: str | None = None) -> dict:
    """Return the part-config (installed parts) of the car you're CURRENTLY
    driving (vid=None). This is what 'change car config' reads first.

    NOTE: fine tuning $vars sliders are not all returned here; for those, edit the
    .pc via read_pc/write_pc.
    """
    try:
        return session.get_tuning(vid=vid)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc)}


# --- 9. set_tuning (GAME, LIVE) ---------------------------------------------
@mcp.tool()
def set_config(cfg: dict, vid: str | None = None) -> dict:
    """Apply a part-config to the car you're CURRENTLY driving (vid=None). The
    core of 'claude, change car config': pass a full or partial part-config tree
    (as from get_config) and it is applied in place.

    NOTE: applying a config RESPAWNS the car at its location (BeamNG repairs it
    and resets damage — exactly like changing parts in the in-game menu).
    """
    try:
        return session.set_tuning(cfg=cfg, vid=vid)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc)}


# --- 10. set_control (GAME, LIVE) -------------------------------------------
@mcp.tool()
def set_control(vid: str | None = None, steering: float | None = None,
                throttle: float | None = None, brake: float | None = None,
                parkingbrake: float | None = None, clutch: float | None = None,
                gear: int | None = None) -> dict:
    """ACTIVE MODE (only when the user explicitly asks Claude to drive): send
    driving inputs to the current car (vid=None). Does nothing unless called.
    """
    try:
        return session.set_control(
            vid=vid, steering=steering, throttle=throttle, brake=brake,
            parkingbrake=parkingbrake, clutch=clutch, gear=gear,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc)}


# --- 11. run_test (GAME, LIVE) ----------------------------------------------
@mcp.tool()
def run_test(vid: str = "ego", model: str = "etk800",
             level: str = "west_coast_usa", ai_mode: str = "span",
             speed_kmh: float = 60.0, duration_s: float = 10.0,
             sample_hz: float = 5.0) -> dict:
    """ACTIVE MODE (drives the car): spawn-if-needed, enable AI, sample telemetry
    for duration_s, return a summary. Only when the user explicitly asks for a
    test run. Timing is wall-clock based.
    """
    try:
        return session.run_test(
            vid=vid, model=model, level=level, ai_mode=ai_mode,
            speed_kmh=speed_kmh, duration_s=duration_s, sample_hz=sample_hz,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc)}


# --- 12. read_pc (OFFLINE, VERIF) -------------------------------------------
@mcp.tool()
def read_pc(model: str, name: str) -> dict:
    """Read a .pc config JSON from the user folder (confined to USER_VEHICLES)."""
    try:
        return pc_config.read_pc(model, name)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc)}


# --- 13. write_pc (OFFLINE, VERIF) ------------------------------------------
@mcp.tool()
def write_pc(model: str, name: str, data: dict) -> dict:
    """Write/overwrite a .pc config JSON into USER_VEHICLES (confined)."""
    try:
        return pc_config.write_pc(model, name, data)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc)}


# --- 14. outgauge_telemetry (GAME for live read, parser VERIF) --------------
@mcp.tool()
def outgauge_telemetry(ip: str = "127.0.0.1", port: int = 4444,
                       timeout: float = 2.0) -> dict:
    """Read ONE OutGauge UDP packet (license-free, no BeamNGpy). Requires
    OutGauge enabled in BeamNG.drive Options > Other > Protocols.
    """
    try:
        data = outgauge.listen_once(ip=ip, port=port, timeout=timeout)
        if data is None:
            return {"ok": True, "received": False}
        out = {
            "speed_kmh": data["speed_kmh"],
            "rpm": data["rpm"],
            "gear": data["gear"],
            "forward_gear": data["forward_gear"],
            "throttle": data["throttle"],
            "brake": data["brake"],
            "clutch": data["clutch"],
            "fuel": data["fuel"],
            "engTemp": data["engTemp"],
            "flags": data["flags"],
            "dashLights": data["dashLights"],
            "showLights": data["showLights"],
        }
        return {"ok": True, "received": True, "data": out}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc)}


if __name__ == "__main__":
    mcp.run()  # stdio transport (default)
