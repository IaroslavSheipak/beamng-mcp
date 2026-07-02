"""Search space: which sliders the sweep may move, and how far.

Built from the car's LIVE tuning surface (``get_tuning_full``), never a
hardcoded list. Auto-selection keeps the lever kinds the engineer trusts and
excludes what a blind optimizer must not touch:

* ``pressure`` — applied live via Lua, not through the respawning config path;
* ``angle_mult`` (camber/toe/caster) — unitless multipliers whose jbeam
  min/max can be REVERSED per part; a search that can't see the car must not
  walk a lever whose direction is untrustworthy.

Live ranges are intersected with the knowledge base's sanity clamps, so a
config slider with a silly jbeam range can't send the search to a wall.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..engineer import knowledge
from ..errors import BeamNGError

#: Auto-selection keeps at most this many levers (sweep budget is laps, not vars).
MAX_VARS = 6
#: Kinds a blind search may walk, in preference order (engineer-trusted first).
KIND_PREFERENCE = ["rate", "lsd", "bias", "height", "aero", "brake"]
#: Kinds never auto-selected (see module docstring).
EXCLUDED_KINDS = {"pressure", "angle_mult"}


@dataclass(frozen=True)
class Param:
    """One tunable slider the sweep may move."""

    var: str
    lo: float
    hi: float
    start: float  # the car's current value (the baseline config)
    kind: str
    title: str | None = None

    @property
    def span(self) -> float:
        return self.hi - self.lo

    def clamp(self, x: float) -> float:
        return self.lo if x < self.lo else self.hi if x > self.hi else x


def _param_from_meta(var: str, meta: dict) -> Param | None:
    spec = knowledge.classify_var(var)
    if spec is None:
        return None
    try:
        lo, hi = float(meta["min"]), float(meta["max"])
        start = float(meta["val"])
    except (TypeError, ValueError, KeyError):
        return None
    lo, hi = min(lo, hi), max(lo, hi)
    klo, khi = spec["clamp"]
    lo, hi = max(lo, klo), min(hi, khi)
    if not (hi > lo):
        return None
    return Param(var=var, lo=lo, hi=hi, start=min(max(start, lo), hi),
                 kind=spec["kind"], title=meta.get("title"))


def build_space(tuning_vars: dict, include: list[str] | None = None,
                max_vars: int = MAX_VARS) -> list[Param]:
    """The sweep's search space from a ``get_tuning_full()['vars']`` map.

    ``include`` pins the exact vars (still range-validated); otherwise levers
    are auto-selected by kind preference, capped at ``max_vars``."""
    if include:
        params: list[Param] = []
        for var in include:
            meta = tuning_vars.get(var)
            if meta is None:
                raise BeamNGError(f"{var} is not a tunable var on this car")
            p = _param_from_meta(var, meta)
            if p is None:
                raise BeamNGError(
                    f"{var} has no usable numeric range (or no KB spec) — "
                    "cannot sweep it")
            params.append(p)
        return params

    by_kind: dict[str, list[Param]] = {}
    for var, meta in sorted(tuning_vars.items()):
        p = _param_from_meta(var, meta)
        if p is None or p.kind in EXCLUDED_KINDS:
            continue
        by_kind.setdefault(p.kind, []).append(p)

    picked: list[Param] = []
    for kind in KIND_PREFERENCE:
        for p in by_kind.get(kind, []):
            if len(picked) >= max_vars:
                return picked
            picked.append(p)
    return picked
