"""Live telemetry: rich per-vehicle classic sensors with graceful fallback.

When the per-vehicle socket is unavailable (e.g. right after a config-change
respawn) the reading falls back to GE state + OutGauge, always tagged with a
``source`` field. Ported from v1 ``telemetry`` with three fixes folded in:
evict the dead handle on the rich-path failure (self-heal), the damage
None-guard (``v.get('damage') or 0``), and the blocking OutGauge recv moved
OUTSIDE the lock so a slow dashboard read can't freeze the recorder/timing threads.
"""

from __future__ import annotations

from ..errors import BeamNGError
from . import outgauge
from .context import Simulator
from .vehicle import evict, resolve_player, use_current

_OG_KEYS = ("speed_kmh", "rpm", "gear", "throttle", "brake", "clutch", "fuel", "engTemp")


def compact_damage(dmg: dict) -> dict:
    """Trim the Damage tree to a total + the parts that are actually damaged.

    None-safe: a present-but-null part ``damage`` no longer raises ``None > 0``.
    """
    pd = dmg.get("part_damage") or {}
    return {
        "total": dmg.get("damage"),
        "lowpressure": dmg.get("lowpressure"),
        "damaged_parts": {
            k: round((v.get("damage") or 0), 3)
            for k, v in pd.items()
            if isinstance(v, dict) and (v.get("damage") or 0) > 0
        },
    }


def outgauge_snapshot() -> dict | None:
    """One OutGauge packet (no per-vehicle socket). None if disabled/busy.

    Does NOT touch BeamNGpy, so it is called outside ``sim.lock``.
    """
    try:
        d = outgauge.listen_once(ip="127.0.0.1", port=4444, timeout=1.2)
    except Exception:
        return None
    if not d:
        return None
    return {k: d.get(k) for k in _OG_KEYS}


def telemetry(sim: Simulator, vid: str | None = None) -> dict:
    """Live telemetry of the current car. Public op — manages its own locking.

    Returns a reading with ``source`` = ``electrics`` (rich) or ``fallback``.
    Raises :class:`BeamNGError` only if BOTH the rich path and the fallback yield
    nothing.
    """
    rich_err: str | None = None
    fb: dict
    with sim.lock:
        target = vid if vid is not None else resolve_player(sim)
        # 1) rich path — per-vehicle classic sensors
        try:
            rvid = use_current(sim, vid)
            v = sim.vehicles[rvid]
            v.poll_sensors()
            st = dict(v.state)
            return {
                "vid": rvid,
                "source": "electrics",
                "electrics": dict(v.sensors["electrics"]),
                "damage": compact_damage(dict(v.sensors["damage"])),
                "gforces": dict(v.sensors["gforces"]),
                "state": {"pos": st.get("pos"), "dir": st.get("dir"), "vel": st.get("vel")},
            }
        except Exception as exc:
            rich_err = repr(exc)
            evict(sim, vid if isinstance(vid, str) else target)
        # 2) fallback — GE state (still BeamNGpy, under the lock)
        fb = {
            "vid": target,
            "source": "fallback",
            "note": (
                "per-vehicle socket unavailable (often right after a config-change "
                "respawn); returned GE state + OutGauge. The handle was evicted, so "
                "the next call re-primes; if it persists, call reconnect()."
            ),
            "rich_error": rich_err,
        }
        try:
            states = sim.bng.vehicles.get_states([target]) if target else {}
            s = states.get(target) if isinstance(states, dict) else None
            if isinstance(s, dict):
                fb["state"] = {"pos": s.get("pos"), "dir": s.get("dir"), "vel": s.get("vel")}
        except Exception:
            pass

    # 3) OutGauge — OUTSIDE the lock (blocking UDP recv, no BeamNGpy)
    og = outgauge_snapshot()
    if og is not None:
        fb["outgauge"] = og
    if "state" not in fb and "outgauge" not in fb:
        raise BeamNGError(rich_err or "telemetry unavailable")
    return fb
