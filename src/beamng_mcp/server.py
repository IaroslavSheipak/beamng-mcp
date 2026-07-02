"""server.py — FastMCP (stdio) tool layer: thin wrappers over App's services.

Scope: NO BeamNG.tech. Classic polled sensors only (Electrics/State/Damage/
GForces) plus license-free OutGauge/MotionSim UDP telemetry. Every tool returns
a JSON-serializable ``{"ok": ...}`` dict and never raises across the MCP
boundary -- :func:`_call` catches the service layer's typed errors
(``BeamNGError``/``LuaError``/``ValueError``/...) into :func:`errors.from_exc`.
Guided workflows (first-time setup, the pit-wall session, the track-day
debrief) ship as MCP prompts and surface as slash commands in clients.

Run:
    beamng-mcp                       (installed console entry point)
    python -m beamng_mcp.server      (from source)
"""

from __future__ import annotations

from collections.abc import Callable

from mcp.server.fastmcp import FastMCP

from .analysis import coach as coach_mod
from .analysis import compare as compare_mod
from .analysis import plots as plots_mod
from .analysis.ingest import load_lap
from .analysis.report import analyze_lap as analyze_lap_file
from .app import APP, App
from .errors import BeamNGError, from_exc, ok
from .sim import doctor as doctor_mod
from .sim import drivelog, lua, outgauge, pc_config, raceline, scenario, tuning, vehicle
from .sim import telemetry as telemetry_svc
from .timing.recorder import latest_lap as latest_rich_lap
from .timing.recorder import recent_laps


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


def create_server(app: App = APP, full: bool | None = None) -> FastMCP:
    """Build a FastMCP server bound to ``app`` (pass a fresh ``App()`` in tests
    to avoid sharing the process-wide singleton).

    Two tool surfaces. ``@core()`` (the default) is the minimal set that covers
    the whole engineer loop with no dead ends: diagnose -> connect -> time laps
    -> analyze/coach -> feel -> plan -> apply -> compare -> save. ``@power()``
    tools (everything else: ACTIVE mode, .pc files, raw Lua, part swapping,
    drive logging, alternate timing modes) register only when ``full`` is true
    — default from ``BEAMNG_FULL_SURFACE=1``. All prompts are always
    registered and reference only core tools.
    """
    if full is None:
        full = app.settings.full_surface
    mcp = FastMCP("beamng-mcp")
    sim = app.sim
    timer = app.timer

    core = mcp.tool  # always registered

    def power() -> Callable:
        """Register only on the full surface; otherwise leave the function
        unregistered (it stays a plain closure, invisible to the client)."""
        return mcp.tool() if full else (lambda fn: fn)

    # === connection lifecycle ================================================
    @core()
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

    @core()
    def disconnect() -> dict:
        """Close the BeamNGpy session and clear all session state."""
        return _call(app.disconnect)

    @core()
    def reconnect() -> dict:
        """Cleanly close and reopen the attach connection — recovers a stale GE
        session (e.g. after the game was restarted). Does NOT clear a game-side
        per-vehicle socket wedge; that needs a BeamNG.drive restart."""
        return _call(app.reconnect)

    @core()
    def status() -> dict:
        """Report connection state, active scenario, spawned vehicles, config."""
        return _call(sim.status)

    @core()
    def doctor() -> dict:
        """Health-check EVERYTHING (run this first when anything misbehaves, or
        for first-time setup): game install + user folder paths, whether the
        game is listening on the tech socket, OutGauge/MotionSim protocol
        settings — including the known settings corruption that silently breaks
        every vehicle spawn game-wide — port collisions, a live OutGauge probe,
        and the beamngpy/game version pairing. Each finding comes with the
        exact fix. Needs no connection."""

        def _doctor() -> dict:
            connected = False
            try:
                connected = bool(sim.status().get("connected"))
            except Exception:  # noqa: BLE001 — status must never block the doctor
                pass
            return doctor_mod.run_doctor(app.settings, connected=connected)

        return _call(_doctor)

    @core()
    def current_vehicles() -> dict:
        """List the vehicles already in the running game, flagging the car the
        user is driving. Read-only; spawns nothing."""
        return _call(vehicle.current_vehicles, sim)

    @power()
    def list_vehicle_models() -> dict:
        """List drivable models from install content/vehicles + user vehicles."""
        return _call(pc_config.list_vehicle_models)

    @power()
    def list_configs(model: str | None = None) -> dict:
        """List available .pc configs in the user vehicles folder."""
        return _call(lambda: {"configs": pc_config.list_pc(model)})

    # === active mode: spawn / drive ==========================================
    @power()
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

    @core()
    def telemetry(vid: str | None = None) -> dict:
        """Poll live telemetry of the car you're CURRENTLY driving (vid=None):
        Electrics channels (rpm, wheelspeed, gear, throttle, brake, fuel,
        oil/water temps, boost, per-wheel brake temps...) plus Damage/GForces
        and State kinematics."""
        return _call(telemetry_svc.telemetry, sim, vid=vid)

    @power()
    def get_config(vid: str | None = None) -> dict:
        """Return the part-config (installed parts + saved tuning vars) of the
        car you're CURRENTLY driving (vid=None). For the FULL tunable surface
        (every $var, not just the saved subset) use get_tuning_full."""
        return _call(tuning.get_tuning, sim, vid=vid)

    @power()
    def set_config(cfg: dict, vid: str | None = None) -> dict:
        """Apply a part-config to the car you're CURRENTLY driving (vid=None):
        pass a full or partial part-config tree (as from get_config).

        NOTE: applying a config RESPAWNS the car at its location (BeamNG
        repairs it and resets damage — exactly like the in-game parts menu)."""
        return _call(tuning.set_tuning, sim, cfg, vid=vid)

    @power()
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

    @power()
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
    @power()
    def read_pc(model: str, name: str) -> dict:
        """Read a .pc config JSON from the user folder (confined)."""
        return _call(pc_config.read_pc, model, name)

    @power()
    def write_pc(model: str, name: str, data: dict) -> dict:
        """Write/overwrite a .pc config JSON into the user folder (confined)."""
        return _call(pc_config.write_pc, model, name, data)

    # === OutGauge / plain drive logging =======================================
    @power()
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

    @power()
    def start_logging() -> dict:
        """Start recording OutGauge telemetry to a CSV (background thread).
        Enable OutGauge in-game first (Options > Other > Protocols,
        127.0.0.1:4444). Robust — does NOT use the per-vehicle socket."""
        return _call(app.drivelog.start)

    @power()
    def stop_logging() -> dict:
        """Stop the active drive recording and return a summary of the run
        (duration, distance, top/avg speed, 0-100, throttle/brake %, gears)."""
        return _call(app.drivelog.stop)

    @power()
    def summarize_drive(path: str | None = None) -> dict:
        """Summarize a recorded drive CSV (defaults to the most recent)."""

        def _summarize() -> dict:
            p = path or drivelog.latest_log(app.settings.logs_dir)
            if not p:
                raise BeamNGError(f"no drive logs found in {app.settings.logs_dir}")
            return drivelog.summarize_csv(p)

        return _call(_summarize)

    @power()
    def vehicle_lua(code: str, vid: str | None = None) -> dict:
        """ADVANCED: run a Lua chunk on the current vehicle and return its value
        (end with `return <expr>`). The deep-introspection hook for analysis —
        query powertrain power/torque, turbo boost, suspension travel, beam
        stress."""
        return _call(lua.vehicle_lua, sim, code, vid=vid)

    # === AI race engineer =====================================================
    @power()
    def start_lap(hz: float = 30.0) -> dict:
        """Begin recording a RICH telemetry lap of the car you're driving:
        speed, lateral/longitudinal/vertical G, yaw heading, steering,
        throttle/brake. Drive your lap, then call stop_lap."""
        return _call(timer.start_lap, hz=hz)

    @power()
    def stop_lap() -> dict:
        """Stop the rich lap recording and auto-analyze it into a car-behavior
        report (grip, balance, braking, ride, symptoms)."""
        return _call(timer.stop_lap)

    @power()
    def lap_status() -> dict:
        """Poll the manual lap recording: is it running, samples so far,
        elapsed time, the CSV path."""
        return _call(timer.lap_status)

    @core()
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

    @core()
    def compare_laps(path_a: str | None = None, path_b: str | None = None) -> dict:
        """Compare two recorded laps — the 'did that setup change actually
        help?' tool. Defaults to the two most recent laps (older = baseline,
        newer = candidate). Returns lap-time delta, per-metric deltas (grip,
        balance, braking, corner speeds) and a plain-language verdict. Pure
        analysis; no game needed."""

        def _compare() -> dict:
            a, b = path_a, path_b
            if a is None and b is None:
                laps = recent_laps(app.settings.logs_dir, 2)
                if len(laps) < 2:
                    raise BeamNGError(
                        f"need two recorded laps in {app.settings.logs_dir} to compare "
                        "— drive a baseline lap and a candidate lap first")
                a, b = laps[0], laps[1]
            elif a is None or b is None:
                given = a or b
                latest = latest_rich_lap(app.settings.logs_dir)
                if not latest or latest == given:
                    raise BeamNGError("only one lap available — pass both paths explicitly")
                a, b = (given, latest) if b is None else (latest, given)
            assert a is not None and b is not None
            return compare_mod.compare_lap_files(a, b)

        return _call(_compare)

    @core()
    def plot_laps(path_a: str | None = None, path_b: str | None = None) -> dict:
        """Render the MoTeC-style lap DEBRIEF as one PNG: delta-T vs distance
        (where the time is gained/lost — THE motorsport chart), the track map
        colored by speed with numbered corners, and the two-lap speed overlay.
        Defaults to the two most recent laps (older = baseline); with one path
        given, renders a single-lap debrief (map + speed + throttle/brake).
        Returns the PNG path. Pure analysis; no game needed."""

        def _plot() -> dict:
            a, b_, err = plots_mod.latest_debrief_paths(
                app.settings.logs_dir, path_a, path_b)
            if err:
                raise BeamNGError(err)
            assert a is not None
            return plots_mod.render_debrief(a, b_)

        return _call(_plot)

    @core()
    def draw_racing_line(path: str | None = None, color_by: str = "speed",
                         ref_path: str | None = None) -> dict:
        """Draw a recorded lap's driven line INTO the game world (GT7-style),
        colored blue<->red: color_by='speed' (RED = slow/braking zones, blue
        = fast — the GT7 convention) or color_by='delta' (needs a reference
        lap: blue where gaining time on it, red where losing — the 'fix THIS
        corner' view). Defaults: latest lap; for 'delta' the reference
        defaults to the lap before it. Remove with clear_racing_line."""

        def _draw() -> dict:
            lap_path = path or latest_rich_lap(app.settings.logs_dir)
            if not lap_path:
                raise BeamNGError(f"no recorded laps found in {app.settings.logs_dir}")
            samples = load_lap(lap_path)
            ref = None
            ref_p = ref_path
            if color_by == "delta" and ref_p is None:
                laps = recent_laps(app.settings.logs_dir, 2)
                ref_p = laps[0] if len(laps) == 2 and laps[1] == lap_path else None
                if not ref_p:
                    raise BeamNGError("color_by='delta' needs a reference lap "
                                      "(pass ref_path)")
            if ref_p:
                ref = load_lap(ref_p)
                if not ref:
                    raise BeamNGError(f"no usable samples in reference {ref_p}")
            segments = raceline.build_segments(samples, color_by=color_by, ref=ref)
            out = app.raceline.draw(sim, segments)
            out.update({"lap": lap_path, "color_by": color_by, "ref": ref_p})
            return out

        return _call(_draw)

    @core()
    def clear_racing_line() -> dict:
        """Remove the drawn racing line from the world."""
        return _call(app.raceline.clear, sim)

    @core()
    def lap_coach(path: str | None = None) -> dict:
        """DRIVING coach (the driver-side twin of race_engineer): reads a
        recorded lap (default: most recent) and returns technique tips —
        braking effort vs the car's proven grip, coasting time, under-driven
        corners, steering sawing — each with confidence + honest caveats. For
        car-side setup changes use race_engineer instead."""

        def _coach() -> dict:
            p = path or latest_rich_lap(app.settings.logs_dir)
            if not p:
                raise BeamNGError(f"no recorded laps found in {app.settings.logs_dir}")
            samples = load_lap(p)
            if not samples:
                raise BeamNGError(f"no usable samples in {p}")
            report = analyze_lap_file(p)
            out = coach_mod.coach(samples, report)
            out["path"] = p
            out["lap"] = {"duration_s": report.get("duration_s"),
                          "distance_m": report.get("distance_m"),
                          "valid": report.get("valid")}
            return out

        return _call(_coach)

    @core()
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

    @core()
    def get_tuning_full() -> dict:
        """Read the car's FULL tunable surface from the vehicle VM (every $var
        with live value + default + min + max + unit + title + category) — far
        richer than get_config's saved-vars subset."""
        return _call(tuning.get_tuning_full, sim)

    @power()
    def set_tuning(vars: dict) -> dict:
        """Apply tuning $vars to the current car, e.g. {"$arb_spring_F": 39600}.
        Values are clamped to the car's live min/max. Applying RESPAWNS the
        car. Use between runs, then re-drive to confirm."""
        return _call(tuning.set_tuning_vars, sim, vars)

    @core()
    def apply_setup(plan: list | None = None, vars: dict | None = None,
                     save_as: str | None = None) -> dict:
        """Apply a race_engineer plan (pass its diagnosis["plan"]) or an
        explicit {"$var": value} map, optionally persisting it as a .pc build
        via save_as. Respawns the car."""
        return _call(app.apply_setup, plan=plan, vars=vars, save_as=save_as)

    @core()
    def set_tire_pressure(psi_f: float | None = None, psi_r: float | None = None) -> dict:
        """Set front/rear tire pressure (psi) LIVE — no respawn."""
        return _call(tuning.set_tire_pressure, sim, psi_f=psi_f, psi_r=psi_r)

    @power()
    def wheel_telemetry() -> dict:
        """Per-wheel data via Lua: name, wheelSpeed, angularVelocity, brake
        surface temperature — for lockup / brake-bias / thermal inference."""
        return _call(tuning.wheel_telemetry, sim)

    # === in-game time trial / auto-lap session ================================
    @core()
    def set_start_line() -> dict:
        """Mark the car's CURRENT position as the start/finish line and draw a
        green gate across the track."""
        return _call(timer.set_start_line)

    @power()
    def start_time_trial(countdown: int = 3, hz: float = 30.0) -> dict:
        """Start an in-game timed lap: 3-2-1-GO countdown, records a rich
        telemetry lap, AUTO-FINISHES on crossing the line again. Non-blocking
        — poll time_trial_status."""
        return _call(timer.start_time_trial, countdown=countdown, hz=hz)

    @power()
    def time_trial_status() -> dict:
        """Poll the time trial: state, live elapsed time, and once finished the
        lap time + summary."""
        return _call(timer.time_trial_status)

    @power()
    def stop_time_trial() -> dict:
        """Finish the current timed lap NOW (manual finish/abort)."""
        return _call(timer.stop_time_trial)

    @core()
    def start_pit_session(hz: float = 30.0) -> dict:
        """THE way to run a session: start the in-game PIT BOARD. Sets the
        start/finish line at the car's position, then the driver JUST DRIVES —
        every flying lap self-times, and after each lap the verdict appears
        IN-GAME as toasts (lap time, validity, balance read, Mara's top setup
        call). No chat round-trips while driving; come back to chat only to
        apply a change or save the build. Stop with stop_pit_session."""
        return _call(app.pitwall.start, hz=hz)

    @core()
    def pit_session_status() -> dict:
        """Pit-board session state: laps timed so far, best, and the full data
        behind the last in-game read (for when the driver comes back to chat)."""
        return _call(app.pitwall.status)

    @core()
    def stop_pit_session() -> dict:
        """End the pit-board session: final lap list + best + the last read."""
        return _call(app.pitwall.stop)

    @core()
    def start_setup_sweep(vars: list[str] | None = None, configs: int = 20,
                          laps_per_config: int = 3, minutes: int = 120,
                          speed_kmh: float = 110.0, aggression: float = 0.85,
                          save_best_as: str | None = None) -> dict:
        """THE OVERNIGHT OPTIMIZER (Canopy-lite): the game's AI drives your car
        as a CONSISTENT robot while a budgeted search (baseline -> Latin
        hypercube -> coordinate descent) walks the car's real tuning sliders,
        scoring each config by its median VALID lap time on the line-crossing
        timer. ACTIVE MODE for a long time — park ON the circuit where the lap
        should start, then launch and walk away. Every result lands in a JSONL
        ledger live; the best config is re-applied (and optionally saved as a
        .pc) at the end no matter what. `vars` pins exact sliders; default
        auto-picks springs/ARBs/diff/brake-bias and NEVER touches camber/toe
        (untrustworthy ranges). Poll sweep_status; abort with stop_setup_sweep."""
        return _call(app.sweep.start, vars=vars, configs=configs,
                     laps_per_config=laps_per_config, minutes=minutes,
                     speed_kmh=speed_kmh, aggression=aggression,
                     save_best_as=save_best_as)

    @core()
    def sweep_status() -> dict:
        """Sweep progress: eval counter, best config + lap time so far, gain vs
        baseline, full history, ledger path."""
        return _call(app.sweep.status)

    @core()
    def stop_setup_sweep() -> dict:
        """Abort the sweep NOW; the best config found so far is still restored
        and the ledger keeps everything already measured."""
        return _call(app.sweep.stop)

    @core()
    def start_lap_session(hz: float = 30.0) -> dict:
        """Begin a HANDS-OFF lap session: set a start/finish line, then just
        DRIVE — every crossing auto-times a lap. Poll lap_session_status.
        (Prefer start_pit_session, which adds the in-game pit board on top.)"""
        return _call(timer.start_lap_session, hz=hz)

    @core()
    def lap_session_status() -> dict:
        """List the auto-timed laps this session, the best, current elapsed."""
        return _call(timer.lap_session_status)

    @core()
    def last_lap() -> dict:
        """The most recent auto-timed lap: lap time + the full telemetry
        report (grip, balance, braking, ride, symptoms)."""
        return _call(timer.last_lap)

    @core()
    def stop_lap_session() -> dict:
        """End the auto-lap session and return the final list of lap times."""
        return _call(timer.stop_lap_session)

    @power()
    def set_traction_control(on: bool) -> dict:
        """Toggle traction control LIVE (no respawn) via the drivingDynamics
        CMU — for an on/off A/B."""
        return _call(tuning.set_traction_control, sim, on)

    # === fitment-aware part editing ===========================================
    @power()
    def list_parts(filter: str | None = None) -> dict:
        """List the car's part slots from the live part TREE. With a `filter`
        it also lists that slot's VALID options (suitablePartNames)."""
        return _call(tuning.list_parts, sim, filter=filter)

    @power()
    def swap_parts(changes: dict) -> dict:
        """Fitment-safe part swap. `changes` = {"slot_id": "part_name"} ("" to
        empty a slot). Validated against suitablePartNames; iterates the
        respawn cascade until it settles."""
        return _call(tuning.swap_parts, sim, changes)

    @core()
    def save_config(name: str) -> dict:
        """Save the car's CURRENT parts + tuning vars as a .pc build (confined
        to the user vehicles folder) so it persists and loads from the in-game
        config menu."""
        return _call(tuning.save_config, sim, name)

    @power()
    def car_mass(without_wheels: bool = False) -> dict:
        """Read the car's ACTUAL mass in kg + center of gravity from the
        physics core. `without_wheels` excludes unsprung wheel mass."""
        return _call(tuning.car_mass, sim, without_wheels=without_wheels)

    @core()
    def clear_gates() -> dict:
        """Wipe ALL start/finish gates + the live timer text from the world."""
        return _call(timer.clear_gates)

    # === guided workflows (MCP prompts — surface as slash commands) ===========
    @mcp.prompt(description="First-time setup: get BeamNG.drive talking to this server")
    def first_time_setup() -> str:
        return (
            "Walk the user through getting BeamNG.drive connected, one step at a time.\n"
            "1. Call doctor() and go through its findings with them — every failed check "
            "comes with the exact fix. Do not continue until the 'tech socket' check passes "
            "(they must launch the game and open the socket from the in-game console: press "
            "~ and run  extensions.load('tech/techCore'); tech_techCore.openServer(25252) ).\n"
            "2. Encourage the OutGauge and MotionSim warnings' fixes (Options > Others > "
            "Protocols) — OutGauge enables drive logging, MotionSim (port 4445) upgrades lap "
            "analysis with true yaw rate.\n"
            "3. Call connect(), then current_vehicles() and telemetry() to prove the loop — "
            "tell them what car they're in and something live (rpm/speed).\n"
            "4. Point at what's next: 'drive and ask me anything — lap timing "
            "(start_lap_session), tuning (race_engineer), or a checkup (doctor)'.\n"
            "Keep each step short; wait for them to confirm before the next."
        )

    @mcp.prompt(description="Full pit-wall session: time laps, describe the feel, tune, prove it")
    def pit_wall_session() -> str:
        return (
            "You are the interface to Mara, the AI race engineer. Run a real pit-wall "
            "session — short, decisive radio calls, no fluff.\n"
            "1. connect() if needed; current_vehicles() + get_tuning_full() to learn the car "
            "and which sliders it actually exposes (never promise a lever it lacks).\n"
            "2. Ask where they're driving and what the goal is (lap time / fix the balance / "
            "build a setup).\n"
            "3. Have them drive to where the lap should start, then start_pit_session() — "
            "the start/finish line is set at the car, every flying lap self-times, and the "
            "verdict (time, validity, balance read, Mara's call) appears IN-GAME as toasts. "
            "Tell them: 'just drive — everything shows on your screen; come back here only "
            "to apply a change'. Poll pit_session_status() when they return.\n"
            "4. After 2-3 laps: last_lap() (or pit_session_status().last_read) for the time + "
            "telemetry report. Give the one-line read (balance tendency, where the grip is). "
            "If the report says the lap is invalid, say so and ask for a clean one — never "
            "coach off a crash lap.\n"
            "5. Ask how the car FELT (entry/mid/exit, brakes, kerbs) and pass their words "
            "verbatim to race_engineer(feedback). Present the ranked plan with its "
            "confidence labels and caveats.\n"
            "6. On approval: apply_setup(plan) — warn that the car respawns in place. ONE "
            "change at a time.\n"
            "7. They re-drive; compare_laps() to prove or bust the change (baseline vs "
            "candidate). Report the verdict honestly — a change that didn't help gets "
            "rolled back, not defended.\n"
            "8. When they're happy: save_config(name) so the build persists in the in-game "
            "config menu. lap_coach() at the end if they want driver-side homework; "
            "plot_laps() for the visual debrief (delta-T + track map), draw_racing_line() "
            "to paint it into the world. If they're done driving for the day, offer "
            "start_setup_sweep() — the overnight robot optimizer.\n"
            "If anything misbehaves (connection, telemetry, spawns): doctor() first."
        )

    @mcp.prompt(description="Debrief what I just drove: lap times, coaching, what changed")
    def track_day_debrief() -> str:
        return (
            "Debrief the user's most recent driving from the recorded data — no game "
            "connection needed.\n"
            "1. analyze_lap() for the latest lap. Lead with the headline numbers: lap "
            "duration, distance, max/avg speed, balance tendency, and whether the lap was "
            "VALID — if not, say why (stopped / too short / crash-contaminated) and treat "
            "everything after as provisional.\n"
            "2. lap_coach() — deliver the driver-side tips in plain language, strongest "
            "evidence first, with each tip's caveat.\n"
            "3. If at least two laps exist: compare_laps() and report the verdict — faster "
            "or slower, and WHICH metrics moved (corner speed, braking, balance).\n"
            "4. Close with one concrete next action: a driving change (from the coach), a "
            "setup change (offer to run race_engineer with how the car felt), or 'drive "
            "more laps' if the data is too thin to trust.\n"
            "Be honest about confidence — never present a low-confidence metric as fact."
        )

    return mcp


mcp = create_server()


def main() -> None:
    mcp.run()  # stdio transport (default)


if __name__ == "__main__":
    main()
