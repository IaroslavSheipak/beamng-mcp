"""Vehicle tuning + parts: read/set ``$vars`` (via part config), live tire
pressure, fitment-aware part swaps, real mass, traction control, raw control.

Service layer: returns plain data, raises ``BeamNGError`` / ``LuaError``; the tool
layer envelopes. Ported from v1 with the part-tree ``walk()`` factored into ONE
helper (v1 reimplemented it ~11x and the copies had diverged), plus the
set_tuning post-respawn recovery, the wheel_telemetry None-guard (now free via
``lua_json``), and the traction-control no-op guard.
"""

from __future__ import annotations

from collections.abc import Callable

from ..errors import BeamNGError, LuaError
from . import lua, pc_config
from .context import Simulator
from .vehicle import use_current

# ---- part-tree helpers (the single walk; v1 had ~11 copies) ----------------


def walk_part_tree(tree: object, visit: Callable[[dict], None]) -> None:
    """Depth-first walk of a ``get_part_config`` partsTree, calling ``visit(node)``
    on each dict node."""

    def _walk(n: object) -> None:
        if isinstance(n, dict):
            visit(n)
            for child in (n.get("children") or {}).values():
                _walk(child)

    _walk(tree)


def parts_summary(tree: object) -> dict:
    """Flatten a part tree to ``{slot_id: chosenPartName}`` for installed parts."""
    out: dict = {}

    def visit(n: dict) -> None:
        sid, ch = n.get("id"), n.get("chosenPartName")
        if sid and ch:
            out[sid] = ch

    if isinstance(tree, dict):
        walk_part_tree(tree, visit)
    return out


def _prepare_vars(vars_map: dict, ranges: dict) -> tuple[dict, dict]:
    """Validate + clamp a ``{$var: value}`` map against live ``{$var: (lo, hi)}``.

    Pure (no game) — returns ``(applied, skipped)``.
    """
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
            fv = max(lo, min(hi, fv))
        applied[k] = fv
    return applied, skipped


def _recover_after_respawn(sim: Simulator, v, vid: str) -> None:
    """set_part_config respawns: re-assert the player pointer + re-prime sensors,
    both guarded so a transient hiccup never masks a config that DID apply."""
    try:
        sim.bng.vehicles.switch(vid)
    except Exception:
        pass
    try:
        v.poll_sensors()
    except Exception:
        pass


# ---- read --------------------------------------------------------------------


def get_tuning(sim: Simulator, vid: str | None = None) -> dict:
    """Compact GE-side config (installed parts + tuning vars). No per-vehicle socket."""
    with sim.lock:
        if vid is None:
            vid = sim.bng.vehicles.get_player_vehicle_id().get("vid")
        info = sim.bng.vehicles.get_current_info(include_config=True)
        vi = info.get(vid) if isinstance(info, dict) else None
        if not vi:
            raise BeamNGError(f"vehicle {vid!r} not in running game")
        cfg = vi.get("config") or {}
        return {
            "vid": vid,
            "model": cfg.get("model") or vi.get("model"),
            "config_file": cfg.get("partConfigFilename"),
            "vars": cfg.get("vars", {}),
            "installed_parts": parts_summary(cfg.get("partsTree")),
        }


def get_tuning_full(sim: Simulator, vid: str | None = None) -> dict:
    """Full ``$var`` surface (val/default/min/max/unit/title/category) from the
    vehicle VM. Raises :class:`LuaError` if the car exposes no variables table."""
    out = lua.run_lua_json(sim, lua.FULL_VARS_LUA, vid)
    data = out["data"]
    if not data.get("ok"):
        raise LuaError(f"vehicle exposes no variables table: {data.get('reason')}")
    return {"vid": out["vid"], "count": data.get("count"), "vars": data.get("vars", {})}


def live_ranges(sim: Simulator, vid: str | None = None) -> dict:
    """``{$var: (lo, hi)}`` from the live surface. Best-effort: ``{}`` if unavailable."""
    try:
        full = get_tuning_full(sim, vid)
    except (LuaError, BeamNGError):
        return {}
    ranges: dict = {}
    for k, meta in full["vars"].items():
        try:
            lo, hi = float(meta.get("min")), float(meta.get("max"))
            ranges[k] = (min(lo, hi), max(lo, hi))
        except (TypeError, ValueError):
            pass
    return ranges


def wheel_telemetry(sim: Simulator, vid: str | None = None) -> dict:
    """Per-wheel probe (name, wheelSpeed, angularVelocity, brakeTemp). The nil-result
    crash v1 had is gone — ``lua_json`` raises :class:`LuaError` instead."""
    out = lua.run_lua_json(sim, lua.WHEELS_LUA, vid)
    return {"vid": out["vid"], "wheels": out["data"].get("wheels", [])}


def car_mass(sim: Simulator, without_wheels: bool = False, vid: str | None = None) -> dict:
    """Real mass (kg) + center of gravity from the physics core."""
    with sim.lock:
        vid = use_current(sim, vid)
        mp = sim.vehicles[vid].get_mass_properties(without_wheels=without_wheels)
    mass = mp.get("mass") if isinstance(mp, dict) else None
    com = mp.get("center_of_gravity") if isinstance(mp, dict) else None
    return {
        "vid": vid,
        "mass_kg": round(mass, 1) if isinstance(mass, (int, float)) else mass,
        "center_of_gravity": com,
        "without_wheels": without_wheels,
        "raw": mp,
    }


# ---- write -------------------------------------------------------------------


def set_control(sim: Simulator, vid: str | None = None, **controls: object) -> dict:
    """Apply raw driving inputs (steering/throttle/brake/...) to the current car."""
    applied = {k: v for k, v in controls.items() if v is not None}
    with sim.lock:
        vid = use_current(sim, vid)
        sim.vehicles[vid].control(**applied)
        return {"vid": vid, "applied": applied}


def set_tuning(sim: Simulator, cfg: dict, vid: str | None = None) -> dict:
    """Apply a full part config (RESPAWNS). Re-asserts player + re-primes sensors."""
    with sim.lock:
        vid = use_current(sim, vid)
        v = sim.vehicles[vid]
        v.set_part_config(cfg)
        _recover_after_respawn(sim, v, vid)
        return {
            "vid": vid,
            "respawned": True,
            "note": "respawn repairs damage; player car re-asserted, sensors re-polled",
        }


def set_tuning_vars(sim: Simulator, vars_map: dict, vid: str | None = None) -> dict:
    """Apply ``$vars`` via set_part_config (RESPAWNS), clamped to the car's live range."""
    if not isinstance(vars_map, dict) or not vars_map:
        raise BeamNGError('vars must be a non-empty {"$var": value} dict')
    ranges = live_ranges(sim, vid)
    applied, skipped = _prepare_vars(vars_map, ranges)
    if not applied:
        raise BeamNGError(f"no applicable $vars (skipped: {skipped})")
    with sim.lock:
        vid = use_current(sim, vid)
        v = sim.vehicles[vid]
        cfg = v.get_part_config()
        varz = cfg.get("vars")
        if not isinstance(varz, dict):
            varz = {}
            cfg["vars"] = varz
        varz.update(applied)
        v.set_part_config(cfg)
        _recover_after_respawn(sim, v, vid)
        return {
            "vid": vid,
            "applied": applied,
            "skipped": skipped,
            "respawned": True,
            "note": "applied via set_part_config — car respawned. Re-drive to confirm.",
        }


def set_tire_pressure(
    sim: Simulator, psi_f: float | None = None, psi_r: float | None = None,
    vid: str | None = None,
) -> dict:
    """Set front/rear tire pressure LIVE (no respawn)."""
    if psi_f is None and psi_r is None:
        raise BeamNGError("pass psi_f and/or psi_r")
    out = lua.run_lua_json(sim, lua.tire_pressure_lua(psi_f, psi_r), vid)
    return {
        "vid": out["vid"],
        "live": True,
        "psi_f": psi_f,
        "psi_r": psi_r,
        "result": out["data"],
        "note": "live pressure change — no respawn. Persists until reload.",
    }


def set_traction_control(sim: Simulator, on: bool, vid: str | None = None) -> dict:
    """Toggle the drivingDynamics/CMU traction-control supervisor LIVE.

    Raises if the car has no CMU (toggled == 0) — a no-op must not read as success,
    or the headline TC-on/off A/B becomes two identical runs.
    """
    out = lua.run_lua_json(sim, lua.traction_control_lua(on), vid)
    data = out["data"]
    toggled = data.get("toggled", 0)
    if not toggled:
        raise BeamNGError(
            "no drivingDynamics/CMU traction-control supervisor on this car — nothing "
            "toggled; a TC on/off A/B is meaningless here"
        )
    return {
        "vid": out["vid"],
        "traction_control": bool(on),
        "toggled": toggled,
        "result": data,
        "note": f"TC {'ENABLED' if on else 'DISABLED'} (live, {toggled} supervisor(s)).",
    }


# ---- parts (fitment-aware) ---------------------------------------------------


def list_parts(sim: Simulator, filter: str | None = None, vid: str | None = None) -> dict:
    """List part slots from the live part tree. With a ``filter`` it also shows each
    slot's valid ``suitablePartNames`` (the authoritative fitment list)."""
    with sim.lock:
        vid = use_current(sim, vid)
        cfg = sim.vehicles[vid].get_part_config()
    f = filter.lower() if filter else None
    out: dict = {}

    def visit(n: dict) -> None:
        sid = n.get("id")
        ch = n.get("chosenPartName")
        suit = n.get("suitablePartNames") or []
        if sid:
            hit = (
                f is None
                or f in sid.lower()
                or f in (ch or "").lower()
                or any(f in (p or "").lower() for p in suit)
            )
            if hit:
                out[sid] = (
                    {"current": ch, "options": suit}
                    if f
                    else {"current": ch, "n_options": len(suit)}
                )

    walk_part_tree(cfg.get("partsTree"), visit)
    return {"vid": vid, "filter": filter, "count": len(out), "slots": out}


def swap_parts(sim: Simulator, changes: dict, vid: str | None = None) -> dict:
    """Fitment-safe part swap. ``changes = {slot_id: part_name}`` (``""`` empties a
    slot). Each choice is validated against the slot's ``suitablePartNames`` and
    applied via set_part_config, iterating the respawn cascade until it settles."""
    if not isinstance(changes, dict) or not changes:
        raise BeamNGError("changes must be a non-empty {slot: part} dict")
    remaining = dict(changes)
    applied: dict = {}
    invalid: dict = {}
    passes = 0
    with sim.lock:
        vid = use_current(sim, vid)
        v = sim.vehicles[vid]
        for _ in range(6):
            if not remaining:
                break
            cfg = v.get_part_config()
            slotmap: dict = {}
            walk_part_tree(
                cfg.get("partsTree"),
                lambda n, m=slotmap: m.__setitem__(n["id"], n) if n.get("id") else None,
            )
            changed = False
            for sid, part in list(remaining.items()):
                node = slotmap.get(sid)
                if node is None:
                    continue  # slot not present yet (may appear after a cascade)
                suit = node.get("suitablePartNames") or []
                if part == "" or part in suit:
                    if node.get("chosenPartName") != part:
                        node["chosenPartName"] = part
                        changed = True
                    applied[sid] = part
                else:
                    invalid[sid] = {"requested": part, "valid_options": suit[:25]}
                del remaining[sid]
            if not changed:
                break
            v.set_part_config(cfg)
            passes += 1
            _recover_after_respawn(sim, v, vid)
    return {
        "vid": vid,
        "applied": applied,
        "invalid": invalid,
        "not_found": dict(remaining),
        "respawns": passes,
        "note": "swapped + validated against suitablePartNames. Tuning vars may have "
        "reset — re-apply with set_tuning.",
    }


def save_config(sim: Simulator, name: str, vid: str | None = None) -> dict:
    """Persist the car's CURRENT parts + tuning vars as a confined ``.pc`` build."""
    with sim.lock:
        vid = use_current(sim, vid)
        v = sim.vehicles[vid]
        cfg = v.get_part_config()
        try:
            info = sim.bng.vehicles.get_current_info(include_config=False).get(vid, {})
            model = info.get("model")
        except Exception:
            model = None
    if not model:
        raise BeamNGError("could not resolve vehicle model")
    pc = {
        "format": 2,
        "model": model,
        "parts": parts_summary(cfg.get("partsTree")) or cfg.get("parts") or {},
        "vars": cfg.get("vars") or {},
    }
    return pc_config.write_pc(model, name, pc)
