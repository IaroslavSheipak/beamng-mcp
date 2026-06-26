"""pc_config.py — pure-stdlib reader/writer for BeamNG .pc vehicle configs.

NO game and NO beamngpy required. All filesystem access for read_pc/write_pc is
strictly confined to USER_VEHICLES (see confinement check in `_confined_target`).
"""

from __future__ import annotations

import json
import os
import re

# --- Resolved paths (Windows-style; server runs under Windows python). ---
# Env overrides first, then hardcoded consumer-build defaults.
GAME_HOME = os.environ.get(
    "BEAMNG_HOME",
    r"C:\Program Files (x86)\Steam\steamapps\common\BeamNG.drive",
)
USERFOLDER = os.environ.get(
    "BEAMNG_USER",
    r"C:\Users\Iaroslav\AppData\Local\BeamNG\BeamNG.drive\current",
)
USER_VEHICLES = os.path.join(USERFOLDER, "vehicles")
INSTALL_VEHICLES = os.path.join(GAME_HOME, "content", "vehicles")

# The -userpath BeamNG wants is the PARENT of the version folder (the dir that
# CONTAINS 'current'/version subdirs), NOT the version folder itself. Passing the
# version folder makes BeamNG spin up an empty first-run profile (EULA screen).
# Only relevant when launching the game ourselves (connect launch=True).
USERPATH_ROOT = os.environ.get("BEAMNG_USERPATH", os.path.dirname(USERFOLDER))

# Allowed characters in a model dir / config name component.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9 _\-.]+$")
_FORBIDDEN = ("/", "\\", ":", "..")


def _reject_component(value: str) -> str | None:
    """Return an error string if `value` is unsafe as a path component, else None."""
    if not value or not value.strip():
        return "empty name"
    for bad in _FORBIDDEN:
        if bad in value:
            return f"illegal sequence {bad!r} in {value!r}"
    if not _SAFE_NAME.match(value):
        return f"illegal characters in {value!r}"
    return None


def vehicle_dir(model: str) -> str:
    """USER_VEHICLES/<model>, validated against path-component injection."""
    err = _reject_component(model)
    if err:
        raise ValueError(err)
    return os.path.join(USER_VEHICLES, model)


def _confined_target(model: str, name: str) -> str:
    """Resolve USER_VEHICLES/<model>/<name>.pc with the confinement rule (section C).

    Raises ValueError if either component is unsafe or the realpath escapes
    USER_VEHICLES (defeats symlink / `..` escape).
    """
    for component in (model, name):
        err = _reject_component(component)
        if err:
            raise ValueError(err)
    filename = name if name.endswith(".pc") else name + ".pc"
    target = os.path.join(USER_VEHICLES, model, filename)
    real = os.path.realpath(target)
    root = os.path.realpath(USER_VEHICLES)
    if os.path.commonpath([real, root]) != root:
        raise ValueError(f"path escapes user vehicles root: {real}")
    return target


def list_pc(model: str | None = None) -> list[dict]:
    """Scan USER_VEHICLES (optionally one model subdir) for *.pc configs.

    Returns a list of {model, name, path}. Never raises on a missing folder.
    """
    out: list[dict] = []
    if not os.path.isdir(USER_VEHICLES):
        return out
    if model is not None:
        err = _reject_component(model)
        if err:
            raise ValueError(err)
        models = [model]
    else:
        models = [
            d
            for d in os.listdir(USER_VEHICLES)
            if os.path.isdir(os.path.join(USER_VEHICLES, d))
        ]
    for m in sorted(models):
        mdir = os.path.join(USER_VEHICLES, m)
        if not os.path.isdir(mdir):
            continue
        for f in sorted(os.listdir(mdir)):
            if f.endswith(".pc"):
                out.append(
                    {
                        "model": m,
                        "name": f[:-3],
                        "path": os.path.join(mdir, f),
                    }
                )
    return out


def read_pc(model: str, name: str) -> dict:
    """Read and json.load a confined .pc config. Returns {ok, model, name, data}."""
    target = _confined_target(model, name)
    if not os.path.isfile(target):
        return {"ok": False, "error": f"no such config: {target}"}
    with open(target, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return {"ok": True, "model": model, "name": os.path.basename(target)[:-3], "data": data}


def validate_pc(data: dict) -> list[str]:
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


def write_pc(model: str, name: str, data: dict) -> dict:
    """Confined write of a .pc config into USER_VEHICLES (section C).

    Returns {ok, path, warnings} or {ok: False, error}.
    """
    try:
        target = _confined_target(model, name)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    if not isinstance(data, dict):
        return {"ok": False, "error": "data must be a JSON object (dict)"}
    try:
        json.dumps(data)
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": f"data not JSON-serializable: {exc!r}"}

    # The parent (model) dir must resolve strictly inside root before we create it.
    parent = os.path.dirname(target)
    real_parent = os.path.realpath(parent)
    root = os.path.realpath(USER_VEHICLES)
    if real_parent != root and os.path.commonpath([real_parent, root]) != root:
        return {"ok": False, "error": f"parent escapes user vehicles root: {real_parent}"}
    os.makedirs(parent, exist_ok=True)

    warnings = validate_pc(data)
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    return {"ok": True, "path": target, "warnings": warnings}
