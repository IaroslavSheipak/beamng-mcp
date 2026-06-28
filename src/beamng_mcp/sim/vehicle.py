"""Per-vehicle handle resolution against a running game.

Attach to the car the player is ALREADY driving, with the priming-retry the
consumer build needs (the first StartVehicleConnection loads tech onto the
vehicle on demand and often raises ``KeyError('result')`` before the port is
reported). Validate cached handles and evict dead ones so a dropped socket
self-heals. Ported from v1 ``_use_current`` + the handle-eviction fix.

All functions assume the caller holds ``sim.lock``.
"""

from __future__ import annotations

import time

from beamngpy import Vehicle
from beamngpy.sensors import Damage, Electrics, GForces

from ..errors import VehicleUnavailable
from .context import Simulator

#: Classic CPU sensors used on the consumer build (State is built in via
#: ``v.state``; Timer is deliberately omitted — its GE handler throws in freeroam).
CLASSIC_SENSORS = ("electrics", "damage", "gforces")


def attach_classic_sensors(v: Vehicle) -> dict:
    """Attach the free classic sensors and return the bundle."""
    bundle = {"electrics": Electrics(), "damage": Damage(), "gforces": GForces()}
    for name, sensor in bundle.items():
        v.attach_sensor(name, sensor)
    return bundle


def evict(sim: Simulator, vid: str | None) -> None:
    """Drop a per-vehicle handle + its sensors so the next ``use_current`` re-runs
    the priming handshake. Call after a poll/Lua/connect failure."""
    if vid:
        sim.vehicles.pop(vid, None)
        sim.sensors.pop(vid, None)


def resolve_player(sim: Simulator) -> str | None:
    """Best-effort vid of the car the player is driving (None if undeterminable).

    After a set_part_config respawn the game can drop the player pointer while the
    car still exists, so fall back to the current vehicles (prefer ``thePlayer``).
    """
    try:
        player = sim.bng.vehicles.get_player_vehicle_id()
        vid = player.get("vid") if isinstance(player, dict) else None
    except Exception:
        vid = None
    if vid:
        return vid
    try:
        cur = sim.bng.vehicles.get_current_info(include_config=False)
    except Exception:
        cur = {}
    if isinstance(cur, dict) and cur:
        return "thePlayer" if "thePlayer" in cur else next(iter(cur))
    return None


def use_current(sim: Simulator, vid: str | None = None, tries: int = 5) -> str:
    """Resolve + connect a usable handle for ``vid`` (player by default).

    Validates a cached handle (``is_connected`` — cheap local check) and evicts it
    if dead, then primes a fresh one with the retry loop. Raises
    :class:`VehicleUnavailable` if no live handle can be obtained.
    """
    if vid is None:
        vid = resolve_player(sim)
        if not vid:
            raise VehicleUnavailable("no active player vehicle in the running game")

    cached = sim.vehicles.get(vid)
    if cached is not None:
        try:
            if cached.is_connected():
                return vid
        except Exception:
            pass
        evict(sim, vid)  # stale/wedged — re-prime below

    last_exc: Exception | None = None
    for _ in range(tries):
        current = sim.bng.vehicles.get_current(include_config=False)
        if vid not in current:
            raise VehicleUnavailable(
                f"vehicle {vid!r} not among current vehicles {list(current.keys())}"
            )
        v = current[vid]  # fresh, unconnected (connection.port is None)
        try:
            v.connect(sim.bng)
            sim.vehicles[vid] = v
            sim.sensors[vid] = attach_classic_sensors(v)
            return vid
        except Exception as exc:  # first attempt primes the vehicle-side extension
            last_exc = exc
            time.sleep(1.0)

    if last_exc is not None and "result" in repr(last_exc):
        raise VehicleUnavailable(
            "per-vehicle socket wedged (KeyError('result') after retries) — a known "
            "BeamNG state after repeated respawns/reconnects. Restart BeamNG, reopen "
            "the tech socket (openServer 25252), and reconnect."
        )
    raise VehicleUnavailable(repr(last_exc) if last_exc else "vehicle connect failed")


def current_vehicles(sim: Simulator) -> dict:
    """List the vehicles present in the running game, flagging the player's car."""
    info = sim.bng.vehicles.get_current_info(include_config=False)
    try:
        player = sim.bng.vehicles.get_player_vehicle_id().get("vid")
    except Exception:
        player = None
    return {
        "player_vid": player,
        "vehicles": [
            {"vid": vid, "model": d.get("model"), "is_player": vid == player}
            for vid, d in info.items()
        ],
    }
