"""session.py — single long-lived BeamNGpy session for the MCP server.

One module-level `Session` singleton holds the connection, the active scenario,
and the spawned-vehicle registry. All beamngpy calls are guarded by a lock.

Scope: BeamNG.drive Steam consumer build (NO BeamNG.tech). Telemetry uses ONLY
the classic polled sensors: Electrics, State, Damage, Timer, GForces. No
tech-gated sensors (Camera/Lidar/Radar/Ultrasonic/AdvancedIMU/Mesh/Powertrain/
GPS/RoadsSensor/IdealRadar) are used anywhere.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time

from beamngpy import BeamNGpy, Scenario, Vehicle
from beamngpy.sensors import Damage, Electrics, GForces

import engineer
import lap_analysis
import lap_telemetry
import outgauge
import pc_config
from logger import LOGS_DIR

DEFAULT_HOST = os.environ.get("BEAMNG_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("BEAMNG_PORT", "25252"))

_HINT = "is BeamNG.drive running and allowed through Windows Firewall?"

# --- Race-engineer telemetry mapping (verified against the game source) -------
# GForces sensor axes (lua/vehicle/sensors.lua + hydros.lua): gx = LATERAL,
# gy = LONGITUDINAL, gz = VERTICAL, in m/s^2 (gravity-inclusive: gz ~= -9.8 at
# rest). lap_analysis wants gx = longitudinal (forward +, decel -), gy = lateral,
# gz = vertical (~+1 g static), all in g. So we SWAP gy<->gx and divide by G.
G = 9.80665
# Signs are sign-checkable live (lap_analysis flags this); flip a constant if a
# known brake/corner shows the wrong sign. Defaults: decel -> negative gx,
# vertical negated so a resting car reads ~+1 g (bump/compression spikes go +).
GF_LONG_SIGN = 1.0   # analysis gx = GF_LONG_SIGN * beamng.gy / G
GF_LAT_SIGN = 1.0    # analysis gy = GF_LAT_SIGN  * beamng.gx / G  (left/right label only)
GF_VERT_SIGN = -1.0  # analysis gz = GF_VERT_SIGN * beamng.gz / G  (~+1 g static)

# The Lua chunk that returns the FULL tunable surface (every $var with live
# val/default/min/max/unit/title/category) from the vehicle VM — what the in-game
# Tuning menu uses. Far richer than GE get_current_info's saved-vars subset.
_FULL_VARS_LUA = (
    "local function cnt(t) local n=0 for _ in pairs(t) do n=n+1 end return n end "
    "if v and v.data and v.data.variables then local out={} "
    "for k,def in pairs(v.data.variables) do out[k]={val=def.val,default=def.default,"
    "min=def.min,max=def.max,unit=def.unit,title=def.title,category=def.category} end "
    "return jsonEncode({ok=true,count=cnt(out),vars=out}) "
    "else return jsonEncode({ok=false,reason='no v.data.variables'}) end"
)

# Per-wheel Lua probe (slip/brake-temp) — the non-hallucinated per-corner path.
_WHEELS_LUA = (
    "local out={} if wheels and wheels.wheels then "
    "for i=0,tableSizeC(wheels.wheels)-1 do local wh=wheels.wheels[i] if wh then "
    "out[#out+1]={name=wh.name,wheelSpeed=wh.wheelSpeed,angularVelocity=wh.angularVelocity,"
    "brakeTemp=wh.brakeSurfaceTemperature,pressureGroup=wh.pressureGroup} end end end "
    "return jsonEncode({ok=true,wheels=out})"
)


def _err(exc: Exception, hint: bool = True) -> dict:
    out = {"ok": False, "error": repr(exc)}
    if hint:
        out["hint"] = _HINT
    return out


def _parts_summary(tree) -> dict:
    """Flatten a part tree to {slot_id: chosenPartName} for installed parts only
    (drops the verbose suitablePartNames lists that bloat get_part_config)."""
    out: dict = {}

    def walk(n):
        if isinstance(n, dict):
            sid, ch = n.get("id"), n.get("chosenPartName")
            if sid and ch:
                out[sid] = ch
            for c in (n.get("children") or {}).values():
                walk(c)

    if isinstance(tree, dict):
        walk(tree)
    return out


class Session:
    """Holds connection + scenario + vehicle/sensor registries."""

    def __init__(self) -> None:
        self.bng: BeamNGpy | None = None
        self.scenario: Scenario | None = None
        self.vehicles: dict[str, Vehicle] = {}
        # vid -> {'electrics':..., 'damage':..., 'timer':..., 'gforces':...}
        self.sensors: dict[str, dict] = {}
        self.home = pc_config.GAME_HOME
        self.user = pc_config.USERFOLDER
        self.host = DEFAULT_HOST
        self.port = DEFAULT_PORT
        self._lock = threading.Lock()
        # Rich per-lap telemetry recorder (race-engineer feature).
        self._lap = lap_telemetry.RichLapRecorder(LOGS_DIR)
        self._lap_vid: str | None = None     # handle cached for the lap recorder

    # ---- helpers -----------------------------------------------------------
    def is_connected(self) -> bool:
        return self.bng is not None

    def _require_conn(self) -> dict | None:
        if not self.is_connected():
            return {"ok": False, "error": "not connected; call connect first"}
        return None

    def _require_vehicle(self, vid: str) -> dict | None:
        if vid not in self.vehicles:
            return {"ok": False, "error": f"no such vehicle {vid}; spawn first"}
        return None

    def _attach_classic_sensors(self, v: Vehicle) -> dict:
        """Attach the classic CPU sensors. State() is attached by Vehicle by
        default (read via v.state), so we only add electrics/damage/gforces.
        NOTE: the Timer sensor is intentionally omitted — its GE handler reads
        scenario_scenarios.getScenario().timer, which is nil in freeroam and
        throws (techCore.lua:337). It is scenario-only and useless here."""
        bundle = {
            "electrics": Electrics(),
            "damage": Damage(),
            "gforces": GForces(),
        }
        for name, sensor in bundle.items():
            v.attach_sensor(name, sensor)
        return bundle

    def _use_current(self, vid=None):
        """Attach to a vehicle that ALREADY exists in the running game (the car
        the player is driving, by default). Does NOT spawn or load anything.
        Caller must hold self._lock. Returns the resolved vid or raises.

        Priming retry: the FIRST StartVehicleConnection to a freeroam car makes
        the game load tech/techCore onto the vehicle on demand, and often returns
        before the vehicle reports its connection port (beamngpy then raises
        KeyError('result')). That failed attempt primes the vehicle-side
        extension, so we retry with a FRESH (port=None) Vehicle each time."""
        if vid is None:
            try:
                player = self.bng.vehicles.get_player_vehicle_id()
                vid = player.get("vid") if isinstance(player, dict) else None
            except Exception:  # noqa: BLE001
                vid = None
            if not vid:
                # After a set_part_config respawn the game can drop the
                # player-vehicle pointer (get_player_vehicle_id -> null) while the
                # car still exists. Fall back to the current vehicle (prefer one
                # named 'thePlayer', else the sole/first vehicle present).
                try:
                    cur = self.bng.vehicles.get_current_info(include_config=False)
                except Exception:  # noqa: BLE001
                    cur = {}
                if isinstance(cur, dict) and cur:
                    vid = "thePlayer" if "thePlayer" in cur else next(iter(cur))
            if not vid:
                raise RuntimeError("no active player vehicle in the running game")
        if vid in self.vehicles:
            return vid
        last_exc = None
        for _ in range(5):
            current = self.bng.vehicles.get_current(include_config=False)
            if vid not in current:
                raise RuntimeError(
                    "vehicle %r not among current vehicles %s"
                    % (vid, list(current.keys())))
            v = current[vid]  # fresh, unconnected (connection.port is None)
            try:
                v.connect(self.bng)
                self.vehicles[vid] = v
                self.sensors[vid] = self._attach_classic_sensors(v)
                return vid
            except Exception as exc:  # noqa: BLE001 — first attempt primes the veh
                last_exc = exc
                time.sleep(1.0)
        # Repeated KeyError('result') after the priming retries == the per-vehicle
        # socket is wedged game-side (known BeamNG state after heavy respawn/
        # reconnect churn). GE-side reads still work; live sensors/Lua do not.
        if last_exc is not None and "result" in repr(last_exc):
            raise RuntimeError(
                "per-vehicle socket wedged (KeyError('result') after %d retries) — "
                "a known BeamNG state after repeated respawns/reconnects. GE reads "
                "still work, but live sensors/Lua need a clean socket. Fix: restart "
                "BeamNG.drive, reopen the tech socket (openServer 25252), reconnect. "
                "To AVOID it, use the persistent MCP connection (one session) rather "
                "than many short-lived reconnects." % 5)
        raise last_exc if last_exc else RuntimeError("vehicle connect failed")

    def current_vehicles(self) -> dict:
        """List the vehicles already present in the running game, flagging the
        player's car. Read-only; spawns nothing."""
        guard = self._require_conn()
        if guard:
            return guard
        with self._lock:
            try:
                info = self.bng.vehicles.get_current_info(include_config=False)
                try:
                    player = self.bng.vehicles.get_player_vehicle_id().get("vid")
                except Exception:  # noqa: BLE001
                    player = None
                vehicles = [
                    {"vid": vid, "model": d.get("model"),
                     "is_player": vid == player}
                    for vid, d in info.items()
                ]
                return {"ok": True, "player_vid": player, "vehicles": vehicles}
            except Exception as exc:  # noqa: BLE001
                return _err(exc)

    # ---- lifecycle ---------------------------------------------------------
    def connect(self, home=None, user=None, host=DEFAULT_HOST, port=DEFAULT_PORT,
                launch=False) -> dict:
        # launch=False (default): ATTACH to a game the user already started with
        # -tcom. We never spawn, load a scenario, or take over. quit_on_close is
        # tied to `launch`, so disconnecting an ATTACHED session leaves the user's
        # game running; only a session we launched ourselves is torn down.
        with self._lock:
            try:
                self.home = home or pc_config.GAME_HOME
                self.user = user or pc_config.USERPATH_ROOT
                self.host = host
                self.port = port
                self.bng = BeamNGpy(host, port, home=self.home, user=self.user,
                                    quit_on_close=launch)
                self.bng.open(launch=launch)
                return {
                    "ok": True,
                    "connected": True,
                    "attached": not launch,
                    "host": host,
                    "port": port,
                }
            except Exception as exc:  # noqa: BLE001
                self.bng = None
                return _err(exc)

    def disconnect(self) -> dict:
        with self._lock:
            if self.bng is None:
                return {"ok": True, "connected": False}
            try:
                self.bng.close()
            except Exception as exc:  # noqa: BLE001
                return _err(exc)
            finally:
                self.bng = None
                self.scenario = None
                self.vehicles = {}
                self.sensors = {}
                self._lap_vid = None
            return {"ok": True, "connected": False}

    def reconnect(self) -> dict:
        """Cleanly close and reopen the GE connection — recovers a stale GE session
        (e.g. after the game was restarted) without manual disconnect/connect.
        Does NOT clear a game-side per-vehicle wedge; that needs a BeamNG restart."""
        home, user, host, port = self.home, self.user, self.host, self.port
        self.disconnect()
        return self.connect(home=home, user=user, host=host, port=port, launch=False)

    def status(self) -> dict:
        return {
            "ok": True,
            "connected": self.is_connected(),
            "host": self.host,
            "port": self.port,
            "home": self.home,
            "user": self.user,
            "scenario": (self.scenario.name if self.scenario is not None else None),
            "vehicles": list(self.vehicles.keys()),
        }

    # ---- vehicles ----------------------------------------------------------
    def _resolve_config(self, model: str, config: str | None) -> str | None:
        if not config:
            return None
        res = pc_config.read_pc(model, config)
        if not res.get("ok"):
            raise ValueError(res.get("error", f"config not found: {config}"))
        # read_pc validated confinement; rebuild the confined path for Vehicle.
        return pc_config._confined_target(model, config)

    def spawn(self, model: str, config=None, vid="ego", pos=(0, 0, 0),
              rot_quat=(0, 0, 0, 1), level="gridmap_v2") -> dict:
        guard = self._require_conn()
        if guard:
            return guard
        with self._lock:
            try:
                config_path = self._resolve_config(model, config)
                v = Vehicle(vid, model=model, part_config=config_path)
                if self.scenario is None:
                    sc = Scenario(level, "mcp_session")
                    sc.add_vehicle(v, pos=tuple(pos), rot_quat=tuple(rot_quat))
                    sc.make(self.bng)
                    self.bng.scenario.load(sc)
                    self.bng.scenario.start()
                    self.scenario = sc
                else:
                    self.bng.vehicles.spawn(v, tuple(pos), rot_quat=tuple(rot_quat))
                self.sensors[vid] = self._attach_classic_sensors(v)
                self.vehicles[vid] = v
                return {
                    "ok": True,
                    "vid": vid,
                    "model": model,
                    "config": config_path,
                    "level": level,
                }
            except Exception as exc:  # noqa: BLE001
                return _err(exc)

    def telemetry(self, vid=None) -> dict:
        """Live telemetry. Tries the rich per-vehicle path (126 Electrics channels
        + Damage + GForces + State); if that socket is unavailable (e.g. right
        after a config-change respawn) it FALLS BACK to GE-side state + OutGauge,
        so a useful reading is always returned (with a `source` field)."""
        guard = self._require_conn()
        if guard:
            return guard
        with self._lock:
            try:
                target = vid or self.bng.vehicles.get_player_vehicle_id().get("vid")
            except Exception:  # noqa: BLE001
                target = vid
            # 1) rich path — per-vehicle classic sensors
            rich_err = None
            try:
                rvid = self._use_current(vid)
                v = self.vehicles[rvid]
                v.poll_sensors()
                st = dict(v.state)
                return {
                    "ok": True,
                    "vid": rvid,
                    "source": "electrics",
                    "electrics": dict(v.sensors["electrics"]),
                    "damage": self._compact_damage(dict(v.sensors["damage"])),
                    "gforces": dict(v.sensors["gforces"]),
                    "state": {"pos": st.get("pos"), "dir": st.get("dir"),
                              "vel": st.get("vel")},
                }
            except Exception as exc:  # noqa: BLE001
                rich_err = repr(exc)
            # 2) fallback — GE state + OutGauge (immune to the per-vehicle socket)
            fb = {"ok": True, "vid": target, "source": "fallback",
                  "note": "per-vehicle telemetry socket unavailable (often right "
                          "after a config-change respawn); returned GE state + "
                          "OutGauge. Recover/reload the car to restore Electrics.",
                  "rich_error": rich_err}
            try:
                states = self.bng.vehicles.get_states([target]) if target else {}
                s = states.get(target) if isinstance(states, dict) else None
                if isinstance(s, dict):
                    fb["state"] = {"pos": s.get("pos"), "dir": s.get("dir"),
                                   "vel": s.get("vel")}
            except Exception:  # noqa: BLE001
                pass
            og = self._outgauge_snapshot()
            if og is not None:
                fb["outgauge"] = og
            if "state" not in fb and "outgauge" not in fb:
                return _err(Exception(rich_err or "telemetry unavailable"))
            return fb

    @staticmethod
    def _compact_damage(dmg: dict) -> dict:
        """Trim the huge Damage tree to a total + the parts that are actually
        damaged (drops the per-beam deform_group_damage internals)."""
        pd = dmg.get("part_damage") or {}
        return {
            "total": dmg.get("damage"),
            "lowpressure": dmg.get("lowpressure"),
            "damaged_parts": {k: round(v.get("damage", 0), 3)
                              for k, v in pd.items()
                              if isinstance(v, dict) and v.get("damage", 0) > 0},
        }

    def _outgauge_snapshot(self):
        """One OutGauge packet (no per-vehicle socket). None if disabled/busy."""
        try:
            d = outgauge.listen_once(ip=self.host, port=4444, timeout=1.2)
        except Exception:  # noqa: BLE001 — port busy / OutGauge off
            return None
        if not d:
            return None
        return {k: d.get(k) for k in ("speed_kmh", "rpm", "gear", "throttle",
                                      "brake", "clutch", "fuel", "engTemp")}

    def vehicle_lua(self, code: str, vid=None) -> dict:
        """Run a Lua chunk on the current vehicle and return its value (end with
        `return <expr>`). Deep-introspection hook for analysis — query powertrain
        power/torque, turbo boost, suspension travel, beam stress, etc. Needs the
        per-vehicle socket (recover the car if it was just respawned)."""
        guard = self._require_conn()
        if guard:
            return guard
        with self._lock:
            try:
                vid = self._use_current(vid)
                resp = self.vehicles[vid].queue_lua_command(code, response=True)
                return {"ok": True, "vid": vid, "result": resp}
            except Exception as exc:  # noqa: BLE001
                return _err(exc)

    def get_tuning(self, vid=None) -> dict:
        """Read the current car's config COMPACTLY and ROBUSTLY via GE-side
        get_current_info (installed parts + tuning vars) — no per-vehicle socket,
        and without the 100k-char suitablePartNames bloat of get_part_config."""
        guard = self._require_conn()
        if guard:
            return guard
        with self._lock:
            try:
                if vid is None:
                    vid = self.bng.vehicles.get_player_vehicle_id().get("vid")
                info = self.bng.vehicles.get_current_info(include_config=True)
                vi = info.get(vid) if isinstance(info, dict) else None
                if not vi:
                    return {"ok": False,
                            "error": "vehicle %r not in running game" % vid}
                cfg = vi.get("config") or {}
                return {
                    "ok": True,
                    "vid": vid,
                    "model": cfg.get("model") or vi.get("model"),
                    "config_file": cfg.get("partConfigFilename"),
                    "vars": cfg.get("vars", {}),
                    "installed_parts": _parts_summary(cfg.get("partsTree")),
                }
            except Exception as exc:  # noqa: BLE001
                return _err(exc)

    def set_tuning(self, cfg: dict, vid=None) -> dict:
        guard = self._require_conn()
        if guard:
            return guard
        with self._lock:
            try:
                vid = self._use_current(vid)
                v = self.vehicles[vid]
                v.set_part_config(cfg)
                # set_part_config respawns (repairs damage); re-sync sensors.
                v.poll_sensors()
                return {
                    "ok": True,
                    "vid": vid,
                    "respawned": True,
                    "note": "respawn repairs damage; sensors re-polled",
                }
            except Exception as exc:  # noqa: BLE001
                return _err(exc)

    def set_control(self, vid=None, **controls) -> dict:
        guard = self._require_conn()
        if guard:
            return guard
        applied = {k: v for k, v in controls.items() if v is not None}
        with self._lock:
            try:
                vid = self._use_current(vid)
                self.vehicles[vid].control(**applied)
                return {"ok": True, "vid": vid, "applied": applied}
            except Exception as exc:  # noqa: BLE001
                return _err(exc)

    def run_test(self, vid="ego", model="etk800", level="west_coast_usa",
                 ai_mode="span", speed_kmh=60.0, duration_s=10.0,
                 sample_hz=5.0) -> dict:
        guard = self._require_conn()
        if guard:
            return guard
        # Spawn if needed (reuses the lock-guarded spawn()).
        if vid not in self.vehicles:
            sp = self.spawn(model=model, vid=vid, level=level)
            if not sp.get("ok"):
                return sp
        # NOTE: the lock is acquired per game interaction (set_mode/poll) and
        # released during time.sleep(), so a long run_test does not monopolize
        # the whole server for its entire duration_s.
        try:
            v = self.vehicles[vid]
            with self._lock:
                v.ai.set_mode(ai_mode)
                v.ai.set_speed(speed_kmh / 3.6, mode="limit")

            samples: list[dict] = []
            interval = 1.0 / sample_hz if sample_hz > 0 else 0.2
            n = max(1, int(round(duration_s * sample_hz)))
            t0 = time.time()
            for _ in range(n):
                with self._lock:
                    v.poll_sensors()
                    e = v.sensors["electrics"]
                    st = dict(v.state)
                samples.append(
                    {
                        "t": round(time.time() - t0, 3),
                        "speed": e.get("wheelspeed"),
                        "rpm": e.get("rpm"),
                        "pos": st.get("pos"),
                    }
                )
                time.sleep(interval)

            with self._lock:
                v.ai.set_mode("disabled")

            speeds = [s["speed"] for s in samples if s["speed"] is not None]
            positions = [s["pos"] for s in samples if s["pos"]]
            distance = 0.0
            for a, b in zip(positions, positions[1:]):
                distance += sum((b[i] - a[i]) ** 2 for i in range(3)) ** 0.5
            final_damage = None
            try:
                with self._lock:
                    final_damage = dict(v.sensors["damage"]).get("damage")
            except Exception:  # noqa: BLE001
                pass
            summary = {
                "max_speed": max(speeds) if speeds else None,
                "avg_speed": (sum(speeds) / len(speeds)) if speeds else None,
                "distance": distance,
                "final_damage": final_damage,
            }
            return {"ok": True, "vid": vid, "samples": samples, "summary": summary}
        except Exception as exc:  # noqa: BLE001
            # Best-effort: try to disable the AI before reporting.
            try:
                with self._lock:
                    self.vehicles[vid].ai.set_mode("disabled")
            except Exception:  # noqa: BLE001
                pass
            return _err(exc)


    # ---- race engineer: rich lap telemetry --------------------------------
    @staticmethod
    def _num(x):
        """Coerce booleans to 0/1, pass numbers through, else None."""
        if isinstance(x, bool):
            return int(x)
        return x if isinstance(x, (int, float)) else None

    def _poll_rich(self) -> dict:
        """poll_fn for RichLapRecorder: one rich telemetry row of the player car.

        Acquires the lock only for the sensor poll (released before the recorder
        sleeps, per the run_test pattern). Maps BeamNG GForces (gx=lateral,
        gy=longitudinal, gz=vertical, m/s^2) onto lap_analysis' convention
        (gx=longitudinal, gy=lateral, gz vertical ~+1 g) in g-units."""
        with self._lock:
            # Reuse the handle primed in start_lap — do NOT re-run the per-vehicle
            # handshake every poll (that churn is what wedges the socket). Only
            # re-resolve if the handle was lost (e.g. a respawn mid-lap).
            vid = self._lap_vid
            if vid is None or vid not in self.vehicles:
                vid = self._use_current(None)
                self._lap_vid = vid
            v = self.vehicles[vid]
            v.poll_sensors()
            e = dict(v.sensors["electrics"])
            gf = dict(v.sensors["gforces"])
            st = dict(v.state)
        pos = st.get("pos") or [None, None, None]
        vel = st.get("vel") or [0.0, 0.0, 0.0]
        d = st.get("dir") or [1.0, 0.0, 0.0]
        speed = math.sqrt(sum((c or 0.0) ** 2 for c in vel))
        heading = math.atan2(d[1] or 0.0, d[0] or 0.0)        # radians
        bgx, bgy, bgz = gf.get("gx") or 0.0, gf.get("gy") or 0.0, gf.get("gz") or 0.0

        def ch(*names):
            for k in names:
                if e.get(k) is not None:
                    return self._num(e[k])
            return None

        return {
            "speed": speed,
            "posx": pos[0], "posy": pos[1], "posz": pos[2],
            "heading": heading,
            "gx": GF_LONG_SIGN * bgy / G,     # longitudinal (forward +, decel -)
            "gy": GF_LAT_SIGN * bgx / G,      # lateral
            "gz": GF_VERT_SIGN * bgz / G,     # vertical (~+1 g static)
            "rpm": ch("rpm"),
            "gear": ch("gear_index", "gear"),
            "throttle": ch("throttle"),
            "brake": ch("brake"),
            "brakeF": ch("brakeF"),
            "brakeR": ch("brakeR"),
            "steering": ch("steering"),
            "steering_input": ch("steering_input"),
            "clutch": ch("clutch"),
            "boost": ch("boost", "turboBoost"),
            "wheelspeed": ch("wheelspeed"),
            "abs_active": ch("abs_active"),
            "tcs_active": ch("tcs_active"),
            "esc_active": ch("esc_active"),
        }

    def start_lap(self, hz: float = 30.0) -> dict:
        """Begin recording a rich telemetry lap of the car you're driving."""
        guard = self._require_conn()
        if guard:
            return guard
        try:                                   # prime the socket so poll 1 is fast
            with self._lock:
                self._lap_vid = self._use_current(None)
        except Exception as exc:  # noqa: BLE001
            return _err(exc)
        return self._lap.start(self._poll_rich, hz=hz)

    def stop_lap(self) -> dict:
        """Stop the lap recording and auto-analyze it (grip / balance / ride)."""
        res = self._lap.stop()
        if res.get("ok") and res.get("path"):
            try:
                rep = lap_analysis.analyze_lap(
                    lap_telemetry.read_lap_csv(res["path"]))
                res["report"] = rep
            except Exception as exc:  # noqa: BLE001
                res["analyze_error"] = repr(exc)
        return res

    def lap_status(self) -> dict:
        return self._lap.status()

    def focus_player(self, vid=None) -> dict:
        """Re-assert player control of a vehicle (bng.vehicles.switch). Fixes the
        'stationary car, controls dead' state a set_part_config respawn can leave
        when the game drops the player-vehicle pointer."""
        guard = self._require_conn()
        if guard:
            return guard
        with self._lock:
            try:
                if vid is None:
                    try:
                        vid = self.bng.vehicles.get_player_vehicle_id().get("vid")
                    except Exception:  # noqa: BLE001
                        vid = None
                    if not vid:
                        cur = self.bng.vehicles.get_current_info(include_config=False)
                        if isinstance(cur, dict) and cur:
                            vid = "thePlayer" if "thePlayer" in cur else next(iter(cur))
                if not vid:
                    return {"ok": False, "error": "no vehicle present to focus"}
                self.bng.vehicles.switch(vid)
                return {"ok": True, "vid": vid,
                        "note": "player control switched to this vehicle"}
            except Exception as exc:  # noqa: BLE001
                return _err(exc)

    def analyze_lap_file(self, path: str | None = None) -> dict:
        """Analyze a recorded lap CSV (default: most recent) into a report."""
        p = path or lap_telemetry.latest_lap(LOGS_DIR)
        if not p:
            return {"ok": False,
                    "error": "no lap recordings in logs/ — run start_lap/stop_lap first"}
        rep = lap_analysis.analyze_lap(lap_telemetry.read_lap_csv(p))
        rep["path"] = p
        return rep

    # ---- race engineer: tuning surface + apply ----------------------------
    def get_tuning_full(self, vid=None) -> dict:
        """Full tunable $var surface (val/default/min/max/unit/title/category) read
        from the vehicle VM's v.data.variables — the in-game Tuning menu's source,
        far richer than get_current_info's saved-vars subset."""
        guard = self._require_conn()
        if guard:
            return guard
        res = self.vehicle_lua(_FULL_VARS_LUA, vid=vid)
        if not res.get("ok"):
            return res
        raw = res.get("result")
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "could not parse vars: %r" % exc, "raw": raw}
        if not isinstance(data, dict) or not data.get("ok"):
            return {"ok": False, "error": "vehicle exposes no variables table",
                    "raw": data}
        return {"ok": True, "vid": res.get("vid"), "count": data.get("count"),
                "vars": data.get("vars", {})}

    def _live_ranges(self, vid=None) -> dict:
        """{$var: (lo, hi)} from the live surface (min/max may be reversed)."""
        full = self.get_tuning_full(vid)
        ranges: dict = {}
        if full.get("ok"):
            for k, meta in full["vars"].items():
                try:
                    lo, hi = float(meta.get("min")), float(meta.get("max"))
                    ranges[k] = (min(lo, hi), max(lo, hi))
                except (TypeError, ValueError):
                    pass
        return ranges

    def set_tuning_vars(self, vars_map: dict, vid=None) -> dict:
        """Apply tuning $vars to the current car via set_part_config (RESPAWNS).

        Values are clamped to the car's live min/max. Reuses the connected vehicle
        handle (avoids the wedge-prone fresh-handshake) per the build notes."""
        guard = self._require_conn()
        if guard:
            return guard
        if not isinstance(vars_map, dict) or not vars_map:
            return {"ok": False, "error": 'vars must be a non-empty {"$var": value} dict'}
        ranges = self._live_ranges(vid)
        applied: dict = {}
        skipped: dict = {}
        for k, val in vars_map.items():
            if not isinstance(k, str) or not k.startswith("$"):
                skipped[k] = "not a $var"
                continue
            try:
                fv = float(val)
            except (TypeError, ValueError):
                skipped[k] = "non-numeric"
                continue
            if k in ranges:
                lo, hi = ranges[k]
                fv = lo if fv < lo else hi if fv > hi else fv
            applied[k] = fv
        if not applied:
            return {"ok": False, "error": "no applicable $vars", "skipped": skipped}
        with self._lock:
            try:
                vid = self._use_current(vid)
                v = self.vehicles[vid]
                cfg = v.get_part_config()
                varz = cfg.get("vars")
                if not isinstance(varz, dict):
                    varz = {}
                    cfg["vars"] = varz
                varz.update(applied)
                v.set_part_config(cfg)        # respawns (repairs/resets)
                # The respawn can drop the player-vehicle pointer (dead controls);
                # re-assert it so the user keeps driving.
                try:
                    self.bng.vehicles.switch(vid)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    v.poll_sensors()
                except Exception:  # noqa: BLE001 — socket re-priming after respawn
                    pass
                return {"ok": True, "vid": vid, "applied": applied, "skipped": skipped,
                        "respawned": True,
                        "note": "applied via set_part_config — car respawned "
                                "(repaired/reset to spawn). Re-drive to confirm."}
            except Exception as exc:  # noqa: BLE001
                return _err(exc)

    def set_tire_pressure(self, psi_f: float | None = None,
                          psi_r: float | None = None, vid=None) -> dict:
        """LIVE tire-pressure change (no respawn) via obj:setGroupPressure on the
        front/rear wheel groups. Pressure is absolute Pa = gauge_psi*6894.757+101325."""
        guard = self._require_conn()
        if guard:
            return guard
        if psi_f is None and psi_r is None:
            return {"ok": False, "error": "pass psi_f and/or psi_r"}
        pf = "nil" if psi_f is None else repr(float(psi_f))
        pr = "nil" if psi_r is None else repr(float(psi_r))
        lua = (
            "local pf,pr=%s,%s local done={} "
            "if wheels and wheels.wheels then "
            "for i=0,tableSizeC(wheels.wheels)-1 do local wh=wheels.wheels[i] "
            "if wh and wh.name then local n=string.upper(wh.name) "
            "local p=nil if string.sub(n,1,1)=='F' and pf then p=pf "
            "elseif string.sub(n,1,1)=='R' and pr then p=pr end "
            "if p and wh.pressureGroup then obj:setGroupPressure(wh.pressureGroup,p*6894.757+101325) "
            "done[#done+1]={wh.name,p} end end end end "
            "return jsonEncode({ok=true,set=done})" % (pf, pr)
        )
        res = self.vehicle_lua(lua, vid=vid)
        if not res.get("ok"):
            return res
        raw = res.get("result")
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:  # noqa: BLE001
            data = {"raw": raw}
        return {"ok": True, "vid": res.get("vid"), "live": True,
                "psi_f": psi_f, "psi_r": psi_r, "result": data,
                "note": "live pressure change — no respawn. Persists until reload."}

    def wheel_telemetry(self, vid=None) -> dict:
        """Per-wheel Lua probe: name, wheelSpeed, angularVelocity, brakeTemp."""
        guard = self._require_conn()
        if guard:
            return guard
        res = self.vehicle_lua(_WHEELS_LUA, vid=vid)
        if not res.get("ok"):
            return res
        raw = res.get("result")
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "parse: %r" % exc, "raw": raw}
        return {"ok": True, "vid": res.get("vid"), "wheels": data.get("wheels", [])}

    # ---- race engineer: the headline orchestration ------------------------
    def race_engineer(self, feedback: str, lap_path: str | None = None,
                      analyze: bool = True) -> dict:
        """THE headline tool. Driver feelings (+ the recorded lap) -> a ranked,
        car-specific setup plan on real tuning $vars, with a pit-wall brief."""
        guard = self._require_conn()
        if guard:
            return guard
        full = self.get_tuning_full()
        available: dict = {}
        if full.get("ok"):
            for k, meta in full["vars"].items():
                try:
                    available[k] = float(meta.get("val"))
                except (TypeError, ValueError):
                    pass
        report = None
        if analyze:
            p = lap_path or lap_telemetry.latest_lap(LOGS_DIR)
            if p:
                try:
                    report = lap_analysis.analyze_lap(lap_telemetry.read_lap_csv(p))
                    report["path"] = p
                except Exception as exc:  # noqa: BLE001
                    report = {"ok": False, "error": repr(exc)}
        live_report = report if (report and report.get("ok")) else None
        diag = engineer.diagnose(feedback or "", live_report, available)
        # Clamp proposals to the car's live min/max and annotate unit/title.
        if full.get("ok"):
            for it in diag.get("plan", []):
                meta = full["vars"].get(it.get("var"))
                if not meta:
                    continue
                try:
                    lo, hi = float(meta["min"]), float(meta["max"])
                    lo, hi = min(lo, hi), max(lo, hi)
                    if it.get("proposed") is not None:
                        it["proposed"] = max(lo, min(hi, it["proposed"]))
                    it["unit"] = meta.get("unit")
                    it["title"] = meta.get("title")
                except (TypeError, ValueError, KeyError):
                    pass
        brief = engineer.format_report(live_report, diag)
        return {"ok": True, "engineer": engineer.ENGINEER, "brief": brief,
                "diagnosis": diag, "report": report, "tunable_vars": len(available)}

    def apply_setup(self, plan: list | None = None, vars: dict | None = None,
                    save_as: str | None = None, vid=None) -> dict:
        """Apply a race_engineer plan (or an explicit {$var:val} map) to the car;
        optionally persist it as a .pc build."""
        guard = self._require_conn()
        if guard:
            return guard
        if isinstance(vars, dict) and vars:
            vmap = vars
        elif isinstance(plan, list):
            vmap = engineer.plan_to_vars(plan, {})
        else:
            vmap = None
        if not vmap:
            return {"ok": False,
                    "error": 'nothing to apply: pass plan=[...] or vars={"$var":value}'}
        res = self.set_tuning_vars(vmap, vid=vid)
        if res.get("ok") and save_as:
            res["saved"] = self._save_pc(save_as, vid=vid)
        return res

    def _save_pc(self, name: str, vid=None) -> dict:
        """Flatten current parts + tuning vars and write a persistent .pc."""
        with self._lock:
            vid = self._use_current(vid)
            v = self.vehicles[vid]
            cfg = v.get_part_config()
        try:
            info = self.bng.vehicles.get_current_info(include_config=False).get(vid, {})
            model = info.get("model")
        except Exception:  # noqa: BLE001
            model = None
        if not model:
            return {"ok": False, "error": "could not resolve vehicle model"}
        parts = _parts_summary(cfg.get("partsTree")) or cfg.get("parts") or {}
        varz = cfg.get("vars") or {}
        pc = {"format": 2, "model": model, "parts": parts, "vars": varz}
        return pc_config.write_pc(model, name, pc)


# Module-level singleton.
session = Session()
