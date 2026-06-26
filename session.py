"""session.py — single long-lived BeamNGpy session for the MCP server.

One module-level `Session` singleton holds the connection, the active scenario,
and the spawned-vehicle registry. All beamngpy calls are guarded by a lock.

Scope: BeamNG.drive Steam consumer build (NO BeamNG.tech). Telemetry uses ONLY
the classic polled sensors: Electrics, State, Damage, Timer, GForces. No
tech-gated sensors (Camera/Lidar/Radar/Ultrasonic/AdvancedIMU/Mesh/Powertrain/
GPS/RoadsSensor/IdealRadar) are used anywhere.
"""

from __future__ import annotations

import os
import threading
import time

from beamngpy import BeamNGpy, Scenario, Vehicle
from beamngpy.sensors import Damage, Electrics, GForces

import outgauge
import pc_config

DEFAULT_HOST = os.environ.get("BEAMNG_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("BEAMNG_PORT", "25252"))

_HINT = "is BeamNG.drive running and allowed through Windows Firewall?"


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
            player = self.bng.vehicles.get_player_vehicle_id()
            vid = player.get("vid")
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
            return {"ok": True, "connected": False}

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


# Module-level singleton.
session = Session()
