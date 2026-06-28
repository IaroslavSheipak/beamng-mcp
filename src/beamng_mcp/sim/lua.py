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
        "tc.setParameters({isEnabled=%s}) n=n+1 end end "
        "return jsonEncode({ok=true,toggled=n,enabled=%s})" % (en, en)
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
