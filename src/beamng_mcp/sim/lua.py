"""Vehicle-VM Lua: the chunks v1 derived against the BeamNG vehicle VM, plus the
ONE JSON request/response contract they all share.

These reach into game internals (``v.data.variables``, ``wheels.wheels``,
``controller.getControllersByType``) — undocumented and version-coupled, but the
only way to read the full tuning surface / per-wheel data / toggle TC on the
consumer build. Ported verbatim. ``lua_json`` unifies the parse step that v1 had
copied four times (one copy missed the None guard -> a wheel_telemetry crash).
"""

from __future__ import annotations

import json

from ..errors import LuaError
from .context import Simulator
from .vehicle import evict, use_current

#: Full tunable ``$var`` surface (val/default/min/max/unit/title/category) from
#: the vehicle VM — the in-game Tuning menu's source.
FULL_VARS_LUA = (
    "local function cnt(t) local n=0 for _ in pairs(t) do n=n+1 end return n end "
    "if v and v.data and v.data.variables then local out={} "
    "for k,def in pairs(v.data.variables) do out[k]={val=def.val,default=def.default,"
    "min=def.min,max=def.max,unit=def.unit,title=def.title,category=def.category} end "
    "return jsonEncode({ok=true,count=cnt(out),vars=out}) "
    "else return jsonEncode({ok=false,reason='no v.data.variables'}) end"
)

#: Per-wheel slip / brake-temp probe (the non-hallucinated per-corner path).
WHEELS_LUA = (
    "local out={} if wheels and wheels.wheels then "
    "for i=0,tableSizeC(wheels.wheels)-1 do local wh=wheels.wheels[i] if wh then "
    "out[#out+1]={name=wh.name,wheelSpeed=wh.wheelSpeed,angularVelocity=wh.angularVelocity,"
    "brakeTemp=wh.brakeSurfaceTemperature,pressureGroup=wh.pressureGroup} end end end "
    "return jsonEncode({ok=true,wheels=out})"
)


def traction_control_lua(on: bool) -> str:
    """Lua to toggle the drivingDynamics/CMU traction-control supervisor live.

    Returns ``{ok,toggled,enabled}`` — ``toggled`` is 0 on a car with no CMU, which
    the caller must treat as a no-op (not success).
    """
    en = "true" if on else "false"
    return (
        "local n=0 for _,cmu in ipairs(controller.getControllersByType("
        "'drivingDynamics/CMU')) do local tc=cmu.getSupervisor and "
        "cmu.getSupervisor('tractionControl') if tc and tc.setParameters then "
        f"tc.setParameters({{isEnabled={en}}}) n=n+1 end end "
        f"return jsonEncode({{ok=true,toggled=n,enabled={en}}})"
    )


def tire_pressure_lua(psi_f: float | None, psi_r: float | None) -> str:
    """Lua to set front/rear tire pressure LIVE (no respawn) via setGroupPressure.

    Absolute Pa = gauge_psi * 6894.757 + 101325. Front/rear are bucketed by the
    wheel name's first letter (F/R). Returns ``{ok, set=[[name, psi], ...]}``.
    """
    pf = "nil" if psi_f is None else repr(float(psi_f))
    pr = "nil" if psi_r is None else repr(float(psi_r))
    return (
        f"local pf,pr={pf},{pr} local done={{}} "
        "if wheels and wheels.wheels then "
        "for i=0,tableSizeC(wheels.wheels)-1 do local wh=wheels.wheels[i] "
        "if wh and wh.name then local n=string.upper(wh.name) "
        "local p=nil if string.sub(n,1,1)=='F' and pf then p=pf "
        "elseif string.sub(n,1,1)=='R' and pr then p=pr end "
        "if p and wh.pressureGroup then obj:setGroupPressure(wh.pressureGroup,p*6894.757+101325) "
        "done[#done+1]={wh.name,p} end end end end "
        "return jsonEncode({ok=true,set=done})"
    )


def lua_json(result: object) -> dict:
    """Parse a ``queue_lua_command(response=True)`` result that returns
    ``jsonEncode(...)``. Raises :class:`LuaError` if the chunk returned nothing,
    non-JSON, or a non-object. The single contract all Lua tools share.
    """
    if result is None:
        raise LuaError("Lua returned no result (chunk errored or socket wedged)")
    if isinstance(result, str):
        try:
            data = json.loads(result)
        except (ValueError, TypeError) as exc:
            raise LuaError(f"Lua result is not JSON: {exc!r}") from exc
    else:
        data = result
    if not isinstance(data, dict):
        raise LuaError(f"Lua result is not an object: {type(data).__name__}")
    return data


def run_lua(sim: Simulator, code: str, vid: str | None = None) -> dict:
    """Run a Lua chunk on the current vehicle VM and return ``{vid, result}``.

    Caller holds ``sim.lock``. Evicts the handle on failure so a wedged socket
    self-heals on the next call.
    """
    vid = use_current(sim, vid)
    try:
        result = sim.vehicles[vid].queue_lua_command(code, response=True)
        return {"vid": vid, "result": result}
    except Exception:
        evict(sim, vid)
        raise


def run_lua_json(sim: Simulator, code: str, vid: str | None = None) -> dict:
    """``run_lua`` + ``lua_json`` — the common case. Returns ``{vid, data}``."""
    out = run_lua(sim, code, vid)
    return {"vid": out["vid"], "data": lua_json(out["result"])}


def vehicle_lua(sim: Simulator, code: str, vid: str | None = None) -> dict:
    """Public op: run a Lua chunk on the current vehicle, return ``{vid, result}``.

    Acquires ``sim.lock``. ``run_lua`` is the lock-free helper used by the tuning
    ops that already hold the lock.
    """
    with sim.lock:
        return run_lua(sim, code, vid)
