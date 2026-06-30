"""server.py — FastMCP (stdio) tool layer: thin wrappers over App's services.

Scope: NO BeamNG.tech. Classic polled sensors only (Electrics/State/Damage/
GForces) plus license-free OutGauge/MotionSim UDP telemetry. Every tool returns
a JSON-serializable ``{"ok": ...}`` dict and never raises across the MCP
boundary -- :func:`_call` catches the service layer's typed errors
(``BeamNGError``/``LuaError``/``ValueError``/...) into :func:`errors.from_exc`.

Run:
    beamng-mcp                       (installed console entry point)
    python -m beamng_mcp.server      (from source)
"""

from __future__ import annotations

from collections.abc import Callable

from mcp.server.fastmcp import FastMCP

from .analysis.report import analyze_lap as analyze_lap_file
from .app import APP, App
from .errors import BeamNGError, from_exc, ok
from .sim import drivelog, lua, outgauge, pc_config, scenario, tuning, vehicle
from .sim import telemetry as telemetry_svc
from .timing.recorder import latest_lap as latest_rich_lap


def _call(fn: Callable[..., object], *args: object, **kwargs: object) -> dict:
    """Run a service call and envelope the result; tools never raise across the
    MCP boundary. A result that already carries an ``ok`` key (``pc_config``
    read/write, the drive logger, ...) passes through unwrapped."""
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — the MCP boundary contract
        return from_exc(exc)
    if isinstance(result, dict) and "ok" in result:
        return result
    return ok(**(result if isinstance(result, dict) else {"result": result}))


def create_server(app: App = APP) -> FastMCP:
    """Build a FastMCP server bound to ``app`` (pass a fresh ``App()`` in tests
    to avoid sharing the process-wide singleton)."""
    mcp = FastMCP("beamng-mcp")
    sim = app.sim
    timer = app.timer

    # === connection lifecycle ================================================
    @mcp.tool()
    def connect(home: str | None = None, user: str | None = None,
                host: str | None = None, port: int | None = None,
                launch: bool = False) -> dict:
        """ATTACH to the BeamNG.drive game the user is ALREADY running (default).

        Does NOT launch the game, load a scenario, or spawn anything — it just
        opens the tech socket so other tools can read/modify the player's
        current car. The user must have opened it in-game first (console `~`):
        extensions.load('tech/techCore'); tech_techCore.openServer(25252).
        Disconnecting an attached session LEAVES THE GAME RUNNING. Pass
        launch=True only to have the server start (and own) its own instance.
        """
        return _call(app.connect, home=home, user=user, host=host, port=port, launch=launch)

    @mcp.tool()
    def disconnect() -> dict:
        """Close the BeamNGpy session and clear all session state."""
        return _call(app.disconnect)

    @mcp.tool()
    def reconnect() -> dict:
        """Cleanly close and reopen the attach connection — recovers a stale GE
        session (e.g. after the game was restarted). Does NOT clear a game-side
        per-vehicle socket wedge; that needs a BeamNG.drive restart."""
        return _call(app.reconnect)

    @mcp.tool()
    def status() -> dict:
        """Report connection state, active scenario, spawned vehicles, config."""
        return _call(sim.status)

    @mcp.tool()
    def current_vehicles() -> dict:
        """List the vehicles already in the running game, flagging the car the
        user is driving. Read-only; spawns nothing."""
        return _call(vehicle.current_vehicles, sim)

    @mcp.tool()
    def list_vehicle_models() -> dict:
        """List drivable models from install content/vehicles + user vehicles."""
        return _call(pc_config.list_vehicle_models)

    @mcp.tool()
    def list_configs(model: str | None = None) -> dict:
        """List available .pc configs in the user vehicles folder."""
        return _call(lambda: {"configs": pc_config.list_pc(model)})

    # === active mode: spawn / drive ==========================================
    @mcp.tool()
    def spawn(model: str, config: str | None = None, vid: str = "ego",
              pos: list[float] | None = None, rot_quat: list[float] | None = None,
              level: str = "gridmap_v2") -> dict:
        """ACTIVE MODE (takes over the session): spawn a NEW vehicle, creating/
        loading a scenario if none is active. NOT used for normal 'change car
        config' — that operates on your current car via get_config/set_config.
        Use spawn only if you explicitly want a fresh test car on a test level.
        """
        return _call(
            scenario.spawn, sim, model, config=config, vid=vid,
            pos=tuple(pos) if pos is not None else (0, 0, 0),
            rot_quat=tuple(rot_quat) if rot_quat is not None else (0, 0, 0, 1),
            level=level,
        )

    @mcp.tool()
    def telemetry(vid: str | None = None) -> dict:
        """Poll live telemetry of the car you're CURRENTLY driving (vid=None):
        Electrics channels (rpm, wheelspeed, gear, throttle, brake, fuel,
        oil/water temps, boost, per-wheel brake temps...) plus Damage/GForces
        and State kinematics."""
        return _call(telemetry_svc.telemetry, sim, vid=vid)

    @mcp.tool()
    def get_config(vid: str | None = None) -> dict:
        """Return the part-config (installed parts + saved tuning vars) of the
        car you're CURRENTLY driving (vid=None). For the FULL tunable surface
        (every $var, not just the saved subset) use get_tuning_full."""
        return _call(tuning.get_tuning, sim, vid=vid)

    @mcp.tool()
    def set_config(cfg: dict, vid: str | None = None) -> dict:
        """Apply a part-config to the car you're CURRENTLY driving (vid=None):
        pass a full or partial part-config tree (as from get_config).

        NOTE: applying a config RESPAWNS the car at its location (BeamNG
        repairs it and resets damage — exactly like the in-game parts menu)."""
        return _call(tuning.set_tuning, sim, cfg, vid=vid)

    @mcp.tool()
    def set_control(vid: str | None = None, steering: float | None = None,
                     throttle: float | None = None, brake: float | None = None,
                     parkingbrake: float | None = None, clutch: float | None = None,
                     gear: int | None = None) -> dict:
        """ACTIVE MODE (only when the user explicitly asks Claude to drive):
        send driving inputs to the current car (vid=None). Does nothing unless
        called."""
        return _call(
            tuning.set_control, sim, vid=vid, steering=steering, throttle=throttle,
            brake=brake, parkingbrake=parkingbrake, clutch=clutch, gear=gear,
        )

    @mcp.tool()
    def run_test(vid: str = "ego", model: str = "etk800",
                 level: str = "west_coast_usa", ai_mode: str = "span",
                 speed_kmh: float = 60.0, duration_s: float = 10.0,
                 sample_hz: float = 5.0) -> dict:
        """ACTIVE MODE (drives the car): spawn-if-needed, enable the BeamNGpy
        AI, sample telemetry for duration_s, return a summary. Only when the
        user explicitly asks for a test run."""
        return _call(
            scenario.run_test, sim, vid=vid, model=model, level=level, ai_mode=ai_mode,
            speed_kmh=speed_kmh, duration_s=duration_s, sample_hz=sample_hz,
        )

    # === .pc configs (offline) ===============================================
    @mcp.tool()
    def read_pc(model: str, name: str) -> dict:
        """Read a .pc config JSON from the user folder (confined)."""
        return _call(pc_config.read_pc, model, name)

    @mcp.tool()
    def write_pc(model: str, name: str, data: dict) -> dict:
        """Write/overwrite a .pc config JSON into the user folder (confined)."""
        return _call(pc_config.write_pc, model, name, data)

    # === OutGauge / plain drive logging =======================================
    @mcp.tool()
    def outgauge_telemetry(ip: str = "127.0.0.1", port: int = 4444,
                            timeout: float = 2.0) -> dict:
        """Read ONE OutGauge UDP packet (license-free, no BeamNGpy). Requires
        OutGauge enabled in BeamNG.drive Options > Other > Protocols."""

        def _read() -> dict:
            data = outgauge.listen_once(ip=ip, port=port, timeout=timeout)
            if data is None:
                return {"received": False}
            keep = ("speed_kmh", "rpm", "gear", "forward_gear", "throttle", "brake",
                    "clutch", "fuel", "engTemp", "flags", "dashLights", "showLights")
            return {"received": True, "data": {k: data[k] for k in keep}}

        return _call(_read)

    @mcp.tool()
    def start_logging() -> dict:
        """Start recording OutGauge telemetry to a CSV (background thread).
        Enable OutGauge in-game first (Options > Other > Protocols,
        127.0.0.1:4444). Robust — does NOT use the per-vehicle socket."""
        return _call(app.drivelog.start)

    @mcp.tool()
    def stop_logging() -> dict:
        """Stop the active drive recording and return a summary of the run
        (duration, distance, top/avg speed, 0-100, throttle/brake %, gears)."""
        return _call(app.drivelog.stop)

    @mcp.tool()
    def summarize_drive(path: str | None = None) -> dict:
        """Summarize a recorded drive CSV (defaults to the most recent)."""

        def _summarize() -> dict:
            p = path or drivelog.latest_log(app.settings.logs_dir)
            if not p:
                raise BeamNGError(f"no drive logs found in {app.settings.logs_dir}")
            return drivelog.summarize_csv(p)

        return _call(_summarize)

    @mcp.tool()
    def vehicle_lua(code: str, vid: str | None = None) -> dict:
        """ADVANCED: run a Lua chunk on the current vehicle and return its value
        (end with `return <expr>`). The deep-introspection hook for analysis —
        query powertrain power/torque, turbo boost, suspension travel, beam
        stress."""
        return _call(lua.vehicle_lua, sim, code, vid=vid)

    # === AI race engineer =====================================================
    @mcp.tool()
    def start_lap(hz: float = 30.0) -> dict:
        """Begin recording a RICH telemetry lap of the car you're driving:
        speed, lateral/longitudinal/vertical G, yaw heading, steering,
        throttle/brake. Drive your lap, then call stop_lap."""
        return _call(timer.start_lap, hz=hz)

    @mcp.tool()
    def stop_lap() -> dict:
        """Stop the rich lap recording and auto-analyze it into a car-behavior
        report (grip, balance, braking, ride, symptoms)."""
        return _call(timer.stop_lap)

    @mcp.tool()
    def analyze_lap(path: str | None = None) -> dict:
        """Analyze a recorded rich lap (default: most recent) into engineer
        metrics: grip envelope, balance/slip angle, braking, ride, symptoms.
        Pure analysis; no game needed once recorded."""

        def _analyze() -> dict:
            p = path or latest_rich_lap(app.settings.logs_dir)
            if not p:
                raise BeamNGError(f"no recorded laps found in {app.settings.logs_dir}")
            return analyze_lap_file(p)

        return _call(_analyze)

    @mcp.tool()
    def race_engineer(feedback: str, lap_path: str | None = None,
                       analyze: bool = True) -> dict:
        """THE race engineer. Tell it how the car FELT in plain language
        ("understeer on entry", "rear's loose on throttle", "bottoming over
        kerbs") and it cross-references your words with the recorded lap
        telemetry and the car's REAL tunable $vars to return a ranked, specific
        setup plan plus a pit-wall brief. Advisory — use apply_setup to apply
        it. Reads the latest lap unless lap_path is given; analyze=False skips
        telemetry and goes on feel alone."""
        return _call(app.race_engineer, feedback, lap_path=lap_path, analyze=analyze)

    @mcp.tool()
    def get_tuning_full() -> dict:
        """Read the car's FULL tunable surface from the vehicle VM (every $var
        with live value + default + min + max + unit + title + category) — far
        richer than get_config's saved-vars subset."""
        return _call(tuning.get_tuning_full, sim)

    @mcp.tool()
    def set_tuning(vars: dict) -> dict:
        """Apply tuning $vars to the current car, e.g. {"$arb_spring_F": 39600}.
        Values are clamped to the car's live min/max. Applying RESPAWNS the
        car. Use between runs, then re-drive to confirm."""
        return _call(tuning.set_tuning_vars, sim, vars)

    @mcp.tool()
    def apply_setup(plan: list | None = None, vars: dict | None = None,
                     save_as: str | None = None) -> dict:
        """Apply a race_engineer plan (pass its diagnosis["plan"]) or an
        explicit {"$var": value} map, optionally persisting it as a .pc build
        via save_as. Respawns the car."""
        return _call(app.apply_setup, plan=plan, vars=vars, save_as=save_as)

    @mcp.tool()
    def set_tire_pressure(psi_f: float | None = None, psi_r: float | None = None) -> dict:
        """Set front/rear tire pressure (psi) LIVE — no respawn."""
        return _call(tuning.set_tire_pressure, sim, psi_f=psi_f, psi_r=psi_r)

    @mcp.tool()
    def wheel_telemetry() -> dict:
        """Per-wheel data via Lua: name, wheelSpeed, angularVelocity, brake
        surface temperature — for lockup / brake-bias / thermal inference."""
        return _call(tuning.wheel_telemetry, sim)

    # === in-game time trial / auto-lap session ================================
    @mcp.tool()
    def set_start_line() -> dict:
        """Mark the car's CURRENT position as the start/finish line and draw a
        green gate across the track."""
        return _call(timer.set_start_line)

    @mcp.tool()
    def start_time_trial(countdown: int = 3, hz: float = 30.0) -> dict:
        """Start an in-game timed lap: 3-2-1-GO countdown, records a rich
        telemetry lap, AUTO-FINISHES on crossing the line again. Non-blocking
        — poll time_trial_status."""
        return _call(timer.start_time_trial, countdown=countdown, hz=hz)

    @mcp.tool()
    def time_trial_status() -> dict:
        """Poll the time trial: state, live elapsed time, and once finished the
        lap time + summary."""
        return _call(timer.time_trial_status)

    @mcp.tool()
    def stop_time_trial() -> dict:
        """Finish the current timed lap NOW (manual finish/abort)."""
        return _call(timer.stop_time_trial)

    @mcp.tool()
    def start_lap_session(hz: float = 30.0) -> dict:
        """Begin a HANDS-OFF lap session: set a start/finish line, then just
        DRIVE — every crossing auto-times a lap. Poll lap_session_status."""
        return _call(timer.start_lap_session, hz=hz)

    @mcp.tool()
    def lap_session_status() -> dict:
        """List the auto-timed laps this session, the best, current elapsed."""
        return _call(timer.lap_session_status)

    @mcp.tool()
    def last_lap() -> dict:
        """The most recent auto-timed lap: lap time + the full telemetry
        report (grip, balance, braking, ride, symptoms)."""
        return _call(timer.last_lap)

    @mcp.tool()
    def stop_lap_session() -> dict:
        """End the auto-lap session and return the final list of lap times."""
        return _call(timer.stop_lap_session)

    @mcp.tool()
    def set_traction_control(on: bool) -> dict:
        """Toggle traction control LIVE (no respawn) via the drivingDynamics
        CMU — for an on/off A/B."""
        return _call(tuning.set_traction_control, sim, on)

    # === fitment-aware part editing ===========================================
    @mcp.tool()
    def list_parts(filter: str | None = None) -> dict:
        """List the car's part slots from the live part TREE. With a `filter`
        it also lists that slot's VALID options (suitablePartNames)."""
        return _call(tuning.list_parts, sim, filter=filter)

    @mcp.tool()
    def swap_parts(changes: dict) -> dict:
        """Fitment-safe part swap. `changes` = {"slot_id": "part_name"} ("" to
        empty a slot). Validated against suitablePartNames; iterates the
        respawn cascade until it settles."""
        return _call(tuning.swap_parts, sim, changes)

    @mcp.tool()
    def save_config(name: str) -> dict:
        """Save the car's CURRENT parts + tuning vars as a .pc build (confined
        to the user vehicles folder) so it persists and loads from the in-game
        config menu."""
        return _call(tuning.save_config, sim, name)

    @mcp.tool()
    def car_mass(without_wheels: bool = False) -> dict:
        """Read the car's ACTUAL mass in kg + center of gravity from the
        physics core. `without_wheels` excludes unsprung wheel mass."""
        return _call(tuning.car_mass, sim, without_wheels=without_wheels)

    @mcp.tool()
    def clear_gates() -> dict:
        """Wipe ALL start/finish gates + the live timer text from the world."""
        return _call(timer.clear_gates)

    return mcp


mcp = create_server()


def main() -> None:
    mcp.run()  # stdio transport (default)


if __name__ == "__main__":
    main()
