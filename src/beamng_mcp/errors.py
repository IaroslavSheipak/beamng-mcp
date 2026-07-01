"""The MCP boundary contract.

Tools return ``{"ok": bool, ...}`` and **never raise across the MCP boundary**
(a raised exception would surface to the client as an opaque ``isError`` result).
Service code raises a typed :class:`BeamNGError`; the tool layer catches it into
:func:`from_exc`. Ported from v1's ``_err`` convention, made typed.
"""

from __future__ import annotations

#: Appended to connection-class errors so the user knows the usual fix.
HINT = (
    "is BeamNG.drive running with the tech socket open? In the in-game console "
    "(~ key): extensions.load('tech/techCore'); tech_techCore.openServer(25252)"
)


class BeamNGError(Exception):
    """Base for expected, envelope-able errors raised by the service layer."""


class NotConnected(BeamNGError):
    """No live BeamNGpy session; call ``connect`` first."""


class VehicleUnavailable(BeamNGError):
    """No usable per-vehicle handle (not spawned, or the socket is wedged)."""


class LuaError(BeamNGError):
    """A vehicle-VM Lua chunk failed or returned an unusable result."""


def ok(**fields: object) -> dict:
    """Build a success envelope."""
    return {"ok": True, **fields}


def err(error: object, *, hint: bool = False, **fields: object) -> dict:
    """Build a failure envelope from a message + optional structured fields."""
    out: dict = {"ok": False, "error": str(error), **fields}
    if hint:
        out["hint"] = HINT
    return out


def from_exc(exc: BaseException, *, hint: bool = True) -> dict:
    """Envelope an exception. ``repr`` keeps the type — matches v1's ``_err``."""
    out: dict = {"ok": False, "error": repr(exc)}
    if hint:
        out["hint"] = HINT
    return out
