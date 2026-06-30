"""Pure-stdlib reader/writer for BeamNG ``.pc`` vehicle configs.

NO game and NO beamngpy required. All filesystem access is strictly confined to
``USER_VEHICLES`` (see :func:`confined_target`). Ported from v1 ``pc_config.py``;
the only change is a ``root`` parameter (defaulting to the configured user
vehicles dir) so the confinement logic is unit-testable against a temp dir.
"""

from __future__ import annotations

import json
import os
import re

from ..config import SETTINGS

_SAFE_NAME = re.compile(r"^[A-Za-z0-9 _\-.]+$")
_FORBIDDEN = ("/", "\\", ":", "..")

#: Known drivable stock models — fallback list for list_vehicle_models when a
#: model's install zip isn't found by name (e.g. content not yet scanned).
STOCK_MODELS = [
    "autobello", "ball", "barstow", "bastion", "bluebuck", "bolide", "burnside",
    "bx", "citybus", "covet", "etk800", "etki", "etkc", "fullsize", "hopper",
    "lansdale", "legran", "midsize", "midtruck", "miramar", "moonhawk", "nine",
    "pessima", "pickup", "roamer", "sbr", "scintilla", "semi", "sunburst",
    "vivace", "wendover", "wigeon",
]


def _root(root: str | None) -> str:
    return root if root is not None else SETTINGS.user_vehicles


def _reject_component(value: str) -> str | None:
    """Return an error string if ``value`` is unsafe as a path component, else None."""
    if not value or not value.strip():
        return "empty name"
    for bad in _FORBIDDEN:
        if bad in value:
            return f"illegal sequence {bad!r} in {value!r}"
    if not _SAFE_NAME.match(value):
        return f"illegal characters in {value!r}"
    return None


def vehicle_dir(model: str, root: str | None = None) -> str:
    """``<root>/<model>``, validated against path-component injection."""
    err = _reject_component(model)
    if err:
        raise ValueError(err)
    return os.path.join(_root(root), model)


def confined_target(model: str, name: str, root: str | None = None) -> str:
    """Resolve ``<root>/<model>/<name>.pc`` with the confinement rule.

    Raises ``ValueError`` if either component is unsafe or the realpath escapes the
    root (defeats symlink / ``..`` escape).
    """
    base = _root(root)
    for component in (model, name):
        err = _reject_component(component)
        if err:
            raise ValueError(err)
    filename = name if name.endswith(".pc") else name + ".pc"
    target = os.path.join(base, model, filename)
    real = os.path.realpath(target)
    real_root = os.path.realpath(base)
    if os.path.commonpath([real, real_root]) != real_root:
        raise ValueError(f"path escapes user vehicles root: {real}")
    return target


def list_pc(model: str | None = None, root: str | None = None) -> list[dict]:
    """Scan the root (optionally one model subdir) for ``*.pc`` configs."""
    base = _root(root)
    out: list[dict] = []
    if not os.path.isdir(base):
        return out
    if model is not None:
        err = _reject_component(model)
        if err:
            raise ValueError(err)
        models = [model]
    else:
        models = [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]
    for m in sorted(models):
        mdir = os.path.join(base, m)
        if not os.path.isdir(mdir):
            continue
        for f in sorted(os.listdir(mdir)):
            if f.endswith(".pc"):
                out.append({"model": m, "name": f[:-3], "path": os.path.join(mdir, f)})
    return out


def read_pc(model: str, name: str, root: str | None = None) -> dict:
    """Read + ``json.load`` a confined ``.pc``. Returns ``{ok, model, name, data}``."""
    target = confined_target(model, name, root)
    if not os.path.isfile(target):
        return {"ok": False, "error": f"no such config: {target}"}
    with open(target, encoding="utf-8") as fh:
        data = json.load(fh)
    return {"ok": True, "model": model, "name": os.path.basename(target)[:-3], "data": data}


def validate_pc(data: object) -> list[str]:
    """Non-fatal validation. Returns a list of human-readable warnings."""
    warnings: list[str] = []
    if not isinstance(data, dict):
        return ["top-level config is not an object"]
    known = {"format", "model", "parts", "vars", "paints", "licenseName"}
    if not (set(data.keys()) & known):
        warnings.append("no recognised top-level keys present")
    if "parts" in data and not isinstance(data["parts"], dict):
        warnings.append("'parts' is not an object")
    if "vars" in data and not isinstance(data["vars"], dict):
        warnings.append("'vars' is not an object")
    return warnings


def write_pc(model: str, name: str, data: object, root: str | None = None) -> dict:
    """Confined write of a ``.pc`` config. Returns ``{ok, path, warnings}`` or
    ``{ok: False, error}``."""
    try:
        target = confined_target(model, name, root)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    if not isinstance(data, dict):
        return {"ok": False, "error": "data must be a JSON object (dict)"}
    try:
        json.dumps(data)
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": f"data not JSON-serializable: {exc!r}"}

    parent = os.path.dirname(target)
    real_parent = os.path.realpath(parent)
    real_root = os.path.realpath(_root(root))
    if real_parent != real_root and os.path.commonpath([real_parent, real_root]) != real_root:
        return {"ok": False, "error": f"parent escapes user vehicles root: {real_parent}"}
    os.makedirs(parent, exist_ok=True)

    warnings = validate_pc(data)
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    return {"ok": True, "path": target, "warnings": warnings}


def list_vehicle_models(install_dir: str | None = None, user_dir: str | None = None) -> dict:
    """Drivable models: install ``content/vehicles`` zips + user vehicle dirs,
    unioned with :data:`STOCK_MODELS`. Pure filesystem scan, no game needed."""
    install = install_dir if install_dir is not None else SETTINGS.install_vehicles
    user = user_dir if user_dir is not None else SETTINGS.user_vehicles
    install_models: set[str] = set()
    if os.path.isdir(install):
        install_models = {f[:-4] for f in os.listdir(install) if f.endswith(".zip")}
    user_models: set[str] = set()
    if os.path.isdir(user):
        user_models = {d for d in os.listdir(user) if os.path.isdir(os.path.join(user, d))}
    all_models = install_models | user_models | set(STOCK_MODELS)
    return {
        "models": sorted(all_models),
        "source_counts": {
            "install": len(install_models),
            "user": len(user_models),
            "stock_fallback": len(STOCK_MODELS),
        },
    }
