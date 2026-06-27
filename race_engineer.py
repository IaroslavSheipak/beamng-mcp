"""race_engineer.py — the AI race-engineer loop as a CLI (and live self-check).

Reuses the SAME session.py code the MCP server exposes, so running this against a
live game is also an end-to-end test of the race-engineer tools. The loop:

    drive a lap  ->  describe the feel  ->  telemetry-grounded setup change

Usage (Windows venv python; game running with the tech socket open on 25252):

    py = .venv\\Scripts\\python.exe
    %py% race_engineer.py vars                 # full tunable $var surface
    %py% race_engineer.py record 60            # record a 60 s lap to logs/lap_*.csv
    %py% race_engineer.py analyze              # analyze the most recent lap
    %py% race_engineer.py advise "understeer on entry, rear loose on throttle"
    %py% race_engineer.py apply '{"$arb_spring_F": 39600}' --save "Engineer v2"
    %py% race_engineer.py pressure 24 26       # live front/rear tire psi (no respawn)
    %py% race_engineer.py swap rally           # fit rally|race coilovers (unlocks rebound)
    %py% race_engineer.py demo                 # end-to-end live self-check

The MCP server exposes the same actions as tools: start_lap / stop_lap /
analyze_lap / race_engineer / set_tuning / apply_setup / set_tire_pressure /
get_tuning_full / wheel_telemetry.
"""
from __future__ import annotations

import json
import sys
import time

# The engineer's brief may contain non-ASCII (e.g. arrows); make the CLI robust
# on non-UTF-8 consoles (the MCP transport is UTF-8, so this is CLI-only).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from session import session  # noqa: E402

# Suspension slot -> {kind: chosenPartName} for the etk800 coilover swap.
_SWAP = {
    "etk800_strut_F": {"rally": "etk800_strut_F_rally", "race": "etk800_strut_F_race"},
    "etk800_spring_R": {"rally": "etk800_spring_R_rally", "race": "etk800_spring_R_race"},
    "etk800_shock_R": {"rally": "etk800_shock_R_rally", "race": "etk800_shock_R_race"},
}


def _pp(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _connect_or_die() -> None:
    res = session.connect()
    if not res.get("ok"):
        print("connect failed: %s" % res.get("error"))
        print("Is BeamNG running with the tech socket open? In the in-game console (~):")
        print("  extensions.load('tech/techCore'); tech_techCore.openServer(25252)")
        sys.exit(1)


def cmd_vars() -> None:
    full = session.get_tuning_full()
    if not full.get("ok"):
        _pp(full)
        return
    print("== %d tunable $vars on the current car ==" % full.get("count", 0))
    by_cat: dict = {}
    for name, m in sorted(full["vars"].items()):
        by_cat.setdefault(m.get("category", "?"), []).append((name, m))
    for cat, items in by_cat.items():
        print("\n[%s]" % cat)
        for name, m in items:
            print("  %-22s = %-8s (%s..%s %s)  %s"
                  % (name, m.get("val"), m.get("min"), m.get("max"),
                     m.get("unit", ""), m.get("title", "")))


def cmd_record(secs: float) -> None:
    start = session.start_lap()
    if not start.get("ok"):
        _pp(start)
        return
    print("recording -> %s   (drive now; %.0fs)" % (start.get("path"), secs))
    t0 = time.time()
    while time.time() - t0 < secs:
        time.sleep(1.0)
        st = session.lap_status()
        print("  %.0fs  %d samples" % (time.time() - t0, st.get("samples", 0)),
              end="\r", flush=True)
    print()
    out = session.stop_lap()
    rep = out.get("report") or {}
    if rep.get("ok"):
        b, g = rep["balance"], rep["grip"]
        print("lap: %d samples, %.0fm, max lat %.2fg, balance %s"
              % (rep["samples"], rep["distance_m"], g["max_lat_g"], b["interpretation"]))
    _pp({k: out.get(k) for k in ("ok", "path", "samples", "duration_s", "poll_error")})


def cmd_analyze(path: str | None) -> None:
    _pp(session.analyze_lap_file(path))


def cmd_advise(feedback: str, path: str | None) -> None:
    res = session.race_engineer(feedback, lap_path=path)
    if not res.get("ok"):
        _pp(res)
        return
    print(res["brief"])
    print("\n-- plan (apply with: race_engineer.py apply '<$var json>') --")
    for it in res["diagnosis"].get("plan", []):
        _pp(it)


def cmd_apply(var_json: str, save_as: str | None) -> None:
    try:
        vmap = json.loads(var_json)
    except Exception as exc:  # noqa: BLE001
        print("bad json: %r" % exc)
        return
    _pp(session.apply_setup(vars=vmap, save_as=save_as))


def cmd_pressure(psi_f: float, psi_r: float) -> None:
    _pp(session.set_tire_pressure(psi_f=psi_f, psi_r=psi_r))


def cmd_swap(kind: str) -> None:
    """Swap the etk800 suspension to rally|race coilovers (unlocks spring/damper)."""
    if kind not in ("rally", "race"):
        print("swap kind must be 'rally' or 'race'")
        return
    g = session._require_conn()
    if g:
        _pp(g)
        return
    with session._lock:
        vid = session._use_current(None)
        v = session.vehicles[vid]
        cfg = v.get_part_config()
        changes = []

        def walk(node):
            if isinstance(node, dict):
                sid = node.get("id")
                if sid in _SWAP:
                    new = _SWAP[sid][kind]
                    if node.get("chosenPartName") != new:
                        changes.append((sid, node.get("chosenPartName"), new))
                        node["chosenPartName"] = new
                for c in (node.get("children") or {}).values():
                    walk(c)

        walk(cfg.get("partsTree"))
        if not changes:
            print("already on %s coilovers (or slots not found)" % kind)
            return
        for sid, old, new in changes:
            print("  %-22s %s -> %s" % (sid, old, new))
        v.set_part_config(cfg)
        try:                                   # keep player control after respawn
            session.bng.vehicles.switch(vid)
        except Exception:  # noqa: BLE001
            pass
    print("swapped to %s coilovers (car respawned). spring/bump/rebound sliders "
          "are now exposed — run `vars` to see them." % kind)


def cmd_save(name: str) -> None:
    """Persist the car's CURRENT parts + tuning vars to a .pc — GE-side only, so
    it works even when the per-vehicle socket is wedged (survives a game restart)."""
    import pc_config
    info = session.bng.vehicles.get_current_info(include_config=True)
    vid = None
    try:
        vid = session.bng.vehicles.get_player_vehicle_id().get("vid")
    except Exception:  # noqa: BLE001
        vid = None
    if not vid or vid not in info:
        vid = "thePlayer" if "thePlayer" in info else (next(iter(info)) if info else None)
    if not vid:
        print("no vehicle present to save")
        return
    cfg = info[vid].get("config", {}) or {}
    model = cfg.get("model") or info[vid].get("model")

    flat: dict = {}

    def walk(node):
        if isinstance(node, dict):
            sid, ch = node.get("id"), node.get("chosenPartName")
            if sid and ch:
                flat[sid] = ch
            for c in (node.get("children") or {}).values():
                walk(c)

    walk(cfg.get("partsTree"))
    pc = {"format": 2, "model": model, "parts": flat, "vars": cfg.get("vars", {}) or {}}
    _pp(pc_config.write_pc(model, name, pc))


def cmd_demo() -> None:
    """End-to-end live self-check against the running car (no full lap needed)."""
    print("1) full tunable surface:")
    full = session.get_tuning_full()
    print("   ok=%s  count=%s" % (full.get("ok"), full.get("count")))
    keys = [k for k in (full.get("vars") or {})
            if any(t in k for t in ("arb", "camber", "lsd", "tirepressure", "brakebias"))]
    print("   sample levers: %s" % ", ".join(sorted(keys)[:8]))

    print("2) record a 4 s sample (exercises the live poll_fn + recorder):")
    st = session.start_lap()
    print("   start ok=%s" % st.get("ok"))
    time.sleep(4.0)
    out = session.stop_lap()
    print("   stopped: %d samples, report.ok=%s"
          % (out.get("samples", 0), (out.get("report") or {}).get("ok")))

    print("3) race_engineer on real vars (feel-only, telemetry optional):")
    res = session.race_engineer("understeer on entry and rear loose on throttle")
    print("   tunable_vars=%s" % res.get("tunable_vars"))
    print(res.get("brief", "(no brief)"))


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 0
    cmd = sys.argv[1]
    _connect_or_die()
    try:
        if cmd == "vars":
            cmd_vars()
        elif cmd == "record":
            cmd_record(float(sys.argv[2]) if len(sys.argv) > 2 else 30.0)
        elif cmd == "analyze":
            cmd_analyze(sys.argv[2] if len(sys.argv) > 2 else None)
        elif cmd == "advise":
            path = sys.argv[3] if len(sys.argv) > 3 else None
            cmd_advise(sys.argv[2] if len(sys.argv) > 2 else "", path)
        elif cmd == "apply":
            save = None
            args = sys.argv[2:]
            if "--save" in args:
                i = args.index("--save")
                save = args[i + 1] if i + 1 < len(args) else None
                args = args[:i]
            cmd_apply(args[0] if args else "{}", save)
        elif cmd == "pressure":
            cmd_pressure(float(sys.argv[2]), float(sys.argv[3]))
        elif cmd == "swap":
            cmd_swap(sys.argv[2] if len(sys.argv) > 2 else "")
        elif cmd == "focus":
            _pp(session.focus_player())
        elif cmd == "save":
            cmd_save(sys.argv[2] if len(sys.argv) > 2 else "Claude Rally Coilover")
        elif cmd == "demo":
            cmd_demo()
        else:
            print("unknown command: %s" % cmd)
            print(__doc__)
    finally:
        session.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
