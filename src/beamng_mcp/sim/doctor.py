"""One-call health check for the whole loop: game, socket, protocols, folders.

Pure stdlib (no beamngpy session needed) so it works BEFORE anything else does
— it exists to be the first call when something misbehaves. It encodes the
expensive live findings from the v2 rebuild (REBUILD.md Phase 4):

* BeamNG's own Protocols UI can persist a NON-numeric string (a stray
  keystroke) into a numeric ``protocols_*`` setting in
  ``<user>/settings/settings.json``. ``lua/vehicle/protocols.lua`` does
  arithmetic on it with no type guard — a fatal vehicle-VM exception that
  disables EVERY vehicle spawn game-wide, looks exactly like corrupted game
  files, and survives a full reinstall (it's a per-profile setting). Numeric-
  looking strings (a port saved as ``"4445"``) are fine — LuaJIT coerces them.
* MotionSim's port defaults to OutGauge's (4444) when its UI field is left
  blank, so the two protocols collide on one UDP socket.

OutGauge enablement persists in ``settings/cloud/settings.json``; MotionSim in
``settings/settings.json`` — both files are scanned.
"""

from __future__ import annotations

import json
import os
import socket
from importlib import metadata

from ..config import Settings
from . import outgauge

TCP_PROBE_TIMEOUT = 1.5
OUTGAUGE_LISTEN_TIMEOUT = 1.5
DEFAULT_PROTOCOL_PORT = 4444  # both OutGauge's default and MotionSim's blank-field fallback

_CONSOLE_FIX = (
    "start BeamNG.drive, then in the in-game console (~ key) run: "
    "extensions.load('tech/techCore'); tech_techCore.openServer({port})"
)


def _check(name: str, status: str, detail: str, fix: str | None = None) -> dict:
    out = {"check": name, "status": status, "detail": detail}
    if fix:
        out["fix"] = fix
    return out


def _load_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _is_numeric(val: object) -> bool:
    try:
        float(val)  # type: ignore[arg-type]
        return True
    except (TypeError, ValueError):
        return False


def _as_port(val: object, default: int) -> int:
    try:
        return int(float(val))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _paths_checks(settings: Settings) -> list[dict]:
    checks: list[dict] = []
    bin64 = os.path.join(settings.game_home, "Bin64")
    if os.path.isdir(bin64):
        checks.append(_check("game install", "ok", f"found {bin64}"))
    else:
        checks.append(_check(
            "game install", "fail",
            f"no Bin64 under {settings.game_home}",
            "point BEAMNG_HOME at the BeamNG.drive install folder (the one containing Bin64)"))
    if os.path.isdir(settings.user_folder):
        checks.append(_check("user folder", "ok", f"found {settings.user_folder}"))
    else:
        checks.append(_check(
            "user folder", "fail",
            f"missing {settings.user_folder}",
            "point BEAMNG_USER at the game's user folder "
            r"(usually %LOCALAPPDATA%\BeamNG\BeamNG.drive\current)"))
    try:
        os.makedirs(settings.logs_dir, exist_ok=True)
        probe = os.path.join(settings.logs_dir, ".doctor_probe")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        checks.append(_check("logs dir", "ok", f"writable: {settings.logs_dir}"))
    except OSError as exc:
        checks.append(_check(
            "logs dir", "fail", f"cannot write {settings.logs_dir}: {exc!r}",
            "set BEAMNG_LOGS_DIR to a writable folder (lap CSVs land there)"))
    return checks


def _socket_check(settings: Settings, connected: bool | None) -> dict:
    if connected:
        return _check("tech socket", "ok", "already attached to the running game")
    fix = _CONSOLE_FIX.format(port=settings.port)
    try:
        with socket.create_connection((settings.host, settings.port), timeout=TCP_PROBE_TIMEOUT):
            pass
        return _check(
            "tech socket", "ok",
            f"the game is listening on {settings.host}:{settings.port} — call connect()")
    except ConnectionRefusedError:
        return _check(
            "tech socket", "fail",
            f"nothing listening on {settings.host}:{settings.port} — the game is not "
            "running, or the tech socket was never opened this session", fix)
    except (TimeoutError, OSError) as exc:
        return _check(
            "tech socket", "fail",
            f"cannot reach {settings.host}:{settings.port} ({exc!r}) — a firewall may "
            "be blocking it", fix)


def _protocol_checks(settings: Settings) -> tuple[list[dict], bool, int]:
    """Scan both settings files. Returns (checks, outgauge_enabled, outgauge_port)."""
    checks: list[dict] = []
    local_path = os.path.join(settings.user_folder, "settings", "settings.json")
    cloud_path = os.path.join(settings.user_folder, "settings", "cloud", "settings.json")
    local, cloud = _load_json(local_path), _load_json(cloud_path)
    if local is None and cloud is None:
        checks.append(_check(
            "protocol settings", "warn",
            f"could not read {local_path} or {cloud_path} — protocol checks skipped "
            "(fresh profile, or BEAMNG_USER points at the wrong folder?)"))
        return checks, True, DEFAULT_PROTOCOL_PORT

    merged: dict = {}
    for d in (local, cloud):
        if d:
            merged.update({k: v for k, v in d.items() if k.startswith("protocols_")})

    # The fatal non-numeric-string corruption ("j" in maxUpdateRate and kin).
    corrupt = [
        (k, v) for k, v in merged.items()
        if isinstance(v, str) and not k.lower().endswith("_ip") and not _is_numeric(v)
    ]
    if corrupt:
        for k, v in corrupt:
            checks.append(_check(
                "protocol settings", "fail",
                f"{k} = {v!r} is a non-numeric string — the game does arithmetic on "
                "this with no type guard, which FATALLY breaks every vehicle spawn "
                "game-wide (it looks exactly like corrupted game files and survives "
                "a reinstall)",
                f"edit {local_path} and set {k} to a number "
                "(BeamNG's default update rate is 60), then restart the game"))
    else:
        checks.append(_check(
            "protocol settings", "ok",
            f"{len(merged)} protocols_* keys scanned, no corrupt values"))

    og_enabled = bool(merged.get("protocols_outgauge_enabled"))
    og_port = _as_port(merged.get("protocols_outgauge_port"), DEFAULT_PROTOCOL_PORT)
    if og_enabled:
        checks.append(_check("OutGauge", "ok", f"enabled in-game (port {og_port})"))
    else:
        checks.append(_check(
            "OutGauge", "warn",
            "not enabled — outgauge_telemetry and the drive logger "
            "(start_logging/stop_logging) will receive nothing",
            "in-game: Options > Others > Protocols > enable OutGauge UDP, "
            f"IP 127.0.0.1, port {DEFAULT_PROTOCOL_PORT}, leave the ID blank"))

    ms_enabled = bool(merged.get("protocols_motionSim_enabled"))
    if ms_enabled:
        ms_port = _as_port(merged.get("protocols_motionSim_port"), DEFAULT_PROTOCOL_PORT)
        if og_enabled and ms_port == og_port:
            checks.append(_check(
                "MotionSim", "warn",
                f"MotionSim and OutGauge are both on UDP port {ms_port} (MotionSim "
                "falls back to OutGauge's port when its field is blank) — the two "
                "streams collide on one socket",
                "in-game: Options > Others > Protocols > set the MotionSim port to "
                "4445 (this server listens there for true yaw rate + clean accel)"))
        else:
            checks.append(_check(
                "MotionSim", "ok",
                f"enabled on port {ms_port} — laps record true yaw rate + "
                "gravity-excluded accel (ms_* columns)"))
    else:
        checks.append(_check(
            "MotionSim", "warn",
            "not enabled — lap analysis falls back to GForces only "
            "(ms_* columns stay blank; analysis still works)",
            "optional: Options > Others > Protocols > enable MotionSim, "
            "IP 127.0.0.1, port 4445, update rate 60"))
    return checks, og_enabled, og_port


def _outgauge_live_check(port: int) -> dict:
    try:
        pkt = outgauge.listen_once(port=port, timeout=OUTGAUGE_LISTEN_TIMEOUT)
    except OSError as exc:
        return _check(
            "OutGauge stream", "warn",
            f"UDP port {port} is busy ({exc!r}) — most likely the drive logger is "
            "recording right now, which is fine")
    if pkt is not None:
        return _check(
            "OutGauge stream", "ok",
            f"live packet received on port {port} ({pkt.get('speed_kmh', 0):.0f} km/h)")
    return _check(
        "OutGauge stream", "warn",
        f"no packet within {OUTGAUGE_LISTEN_TIMEOUT:.0f} s on port {port} — the game "
        "only streams while you are in a vehicle (menus/pause send nothing)")


def _beamngpy_check() -> dict:
    try:
        ver = metadata.version("beamngpy")
    except metadata.PackageNotFoundError:
        return _check("beamngpy", "fail", "beamngpy is not installed in this environment",
                      "pip install beamngpy==1.35.1")
    return _check(
        "beamngpy", "ok",
        f"beamngpy {ver} (pin the minor to the game's: game 0.38.x <-> beamngpy 1.35.x; "
        "a Steam auto-update that bumps the game minor breaks the handshake until "
        "beamngpy is re-pinned)")


def run_doctor(settings: Settings, connected: bool | None = None,
               probe_outgauge: bool = True) -> dict:
    """Run every check. ``connected`` short-circuits the TCP probe when the
    session is already attached; ``probe_outgauge=False`` skips the (blocking)
    UDP listen — used by tests and anything latency-sensitive."""
    checks = _paths_checks(settings)
    if settings.full_surface:
        checks.append(_check(
            "tool surface", "ok",
            "FULL — all tools exposed (ACTIVE mode, .pc files, raw Lua, part "
            "swapping, drive logging, alternate timing modes)"))
    else:
        checks.append(_check(
            "tool surface", "ok",
            "core — the essential engineer loop only; set BEAMNG_FULL_SURFACE=1 "
            "in the server's environment to expose the full tool surface"))
    checks.append(_beamngpy_check())
    sock = _socket_check(settings, connected)
    checks.append(sock)
    proto, og_enabled, og_port = _protocol_checks(settings)
    checks.extend(proto)
    if probe_outgauge and og_enabled and sock["status"] == "ok":
        checks.append(_outgauge_live_check(og_port))

    fails = [c for c in checks if c["status"] == "fail"]
    warns = [c for c in checks if c["status"] == "warn"]
    if fails:
        summary = f"{len(fails)} problem(s) blocking you — fix those first"
    elif warns:
        summary = f"ready, with {len(warns)} thing(s) worth knowing"
    else:
        summary = "everything checks out — connect() and drive"
    return {"ok": not fails, "summary": summary, "checks": checks}
