"""smoke_test.py — offline self-check (NO game launched).

Run with the Windows venv python:
    C:\\Users\\Iaroslav\\beamng-mcp\\.venv\\Scripts\\python.exe smoke_test.py

Exits non-zero on any failure.
"""

from __future__ import annotations

import inspect
import struct
import sys


def main() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        if cond:
            print(f"PASS  {name}")
        else:
            print(f"FAIL  {name}  {detail}")
            failures.append(name)

    # 1. imports -------------------------------------------------------------
    from beamngpy import BeamNGpy, Scenario, Vehicle  # noqa: F401
    from beamngpy.sensors import (  # noqa: F401
        Damage, Electrics, GForces, State, Timer,
    )
    from mcp.server.fastmcp import FastMCP  # noqa: F401
    check("imports", True)

    # 2. signatures match API_SURFACE ---------------------------------------
    bng_params = inspect.signature(BeamNGpy.__init__).parameters
    check("BeamNGpy.__init__ host/port/home/user",
          all(p in bng_params for p in ("host", "port", "home", "user")),
          str(list(bng_params)))
    ctrl_params = inspect.signature(Vehicle.control).parameters
    check("Vehicle.control inputs",
          all(p in ctrl_params for p in
              ("steering", "throttle", "brake", "parkingbrake", "clutch", "gear")),
          str(list(ctrl_params)))

    # 3. Electrics subclasses dict; no-arg construct -------------------------
    check("Electrics() subclasses dict", dict in Electrics().__class__.__mro__)
    check("classic sensors construct no-args",
          all(isinstance(s(), dict) for s in (Electrics, Damage, Timer, GForces, State)))

    # 4. read_pc loads bx/race with parts & vars -----------------------------
    import pc_config
    res = pc_config.read_pc("bx", "race")
    if not res.get("ok"):
        check("read_pc bx/race", False, str(res))
    else:
        data = res["data"]
        check("read_pc bx/race has parts & vars",
              "parts" in data and "vars" in data, str(list(data.keys())))

    # 5. write_pc confinement ------------------------------------------------
    evil = pc_config.write_pc("bx", "../evil", {})
    check("write_pc rejects ../evil", evil.get("ok") is False, str(evil))
    tmp = pc_config.write_pc("bx", "smoke_test_tmp",
                             {"format": 2, "model": "bx", "parts": {}, "vars": {}})
    check("write_pc allows confined name", tmp.get("ok") is True, str(tmp))
    if tmp.get("ok"):
        import os
        try:
            os.remove(tmp["path"])
        except OSError:
            pass

    # 6. outgauge.parse roundtrip (92 and 96 byte) ---------------------------
    import outgauge
    packed92 = struct.pack(
        outgauge.FMT_92,
        1234,            # time_ms
        b"bx\x00\x00",  # car
        0,               # flags
        4,               # gear (Reverse=0,Neutral=1,First=2 -> 4 = third)
        0,               # plid
        25.0,            # speed m/s
        3500.0,          # rpm
        0.0, 90.0, 0.5, 1.0, 95.0,  # turbo,engTemp,fuel,oilPressure,oilTemp
        0,               # dashLights
        0,               # showLights
        0.8, 0.0, 0.0,  # throttle,brake,clutch
        b"D1\x00", b"D2\x00",
    )
    p92 = outgauge.parse(packed92)
    check("outgauge.parse 92 speed", abs(p92["speed_kmh"] - 25.0 * 3.6) < 1e-3, str(p92.get("speed_kmh")))
    check("outgauge.parse 92 rpm", abs(p92["rpm"] - 3500.0) < 1e-3, str(p92.get("rpm")))
    check("outgauge.parse 92 gear", p92["gear"] == 4, str(p92.get("gear")))
    packed96 = packed92 + struct.pack("<i", 7)
    p96 = outgauge.parse(packed96)
    check("outgauge.parse 96 has id", p96.get("id") == 7, str(p96.get("id")))

    # 7. server registers the full tool surface -----------------------------
    import server
    tools = server.mcp._tool_manager.list_tools()
    names = {t.name for t in tools}
    check("server registers >=40 tools", len(tools) >= 40, f"got {len(tools)}")
    check("expected tools present",
          {"connect", "current_vehicles", "telemetry", "get_config",
           "set_config", "start_logging", "stop_logging", "summarize_drive",
           "vehicle_lua",
           # timing + parts subsystems (kept in sync with the README tool table)
           "start_time_trial", "start_lap_session", "set_traction_control",
           "list_parts", "swap_parts", "car_mass", "clear_gates"} <= names,
          str(sorted(names)))

    # 8. logger.summarize_csv on a synthetic drive --------------------------
    import logger
    import tempfile
    tmpcsv = os.path.join(tempfile.gettempdir(), "smoke_drive.csv")
    with open(tmpcsv, "w", newline="") as fh:
        fh.write("t,speed_kmh,rpm,gear,throttle,brake,clutch,fuel,engTemp\n")
        for i in range(20):
            fh.write("%.2f,%.1f,%d,2,1.0,0.0,0.0,%.3f,90\n"
                     % (i * 0.1, i * 5.0, 2000 + i * 100, 1.0 - i * 0.001))
    s = logger.summarize_csv(tmpcsv)
    check("logger.summarize_csv works",
          s.get("ok") and s.get("samples") == 20, str(s)[:200])
    try:
        os.remove(tmpcsv)
    except OSError:
        pass

    # 9. race-engineer pipeline (offline, synthetic lap) --------------------
    import csv as _csv
    import engineer
    import engineer_kb
    import lap_analysis
    import lap_telemetry

    check("race-engineer tools present",
          {"start_lap", "stop_lap", "analyze_lap", "race_engineer",
           "get_tuning_full", "set_tuning", "apply_setup",
           "set_tire_pressure", "wheel_telemetry"} <= names,
          str(sorted(names)))

    # KB sign-convention monotonicity (raises on any backwards-advice sign flip).
    try:
        engineer_kb._selftest()
        kb_ok, kb_err = True, ""
    except Exception as exc:  # noqa: BLE001
        kb_ok, kb_err = False, repr(exc)
    check("engineer_kb sign conventions (no backwards advice)", kb_ok, kb_err)

    # Synthetic understeer lap -> RICH_FIELDS csv -> read back -> analyze.
    us_rows = lap_analysis._synth_corner(0.8)
    lapcsv = os.path.join(tempfile.gettempdir(), "smoke_lap.csv")
    with open(lapcsv, "w", newline="") as fh:
        w = _csv.writer(fh)
        w.writerow(lap_telemetry.RICH_FIELDS)
        for r in us_rows:
            w.writerow([r.get(f, "") for f in lap_telemetry.RICH_FIELDS])
    rep = lap_analysis.analyze_lap(lap_telemetry.read_lap_csv(lapcsv))
    check("analyze_lap detects understeer on synthetic lap",
          rep.get("ok") and rep["balance"]["overall_index"] > 0
          and any(s["symptom"] == "understeer" for s in rep["symptoms"]),
          str(rep.get("balance")))

    # Full diagnose -> plan -> $var map against a realistic available-vars dict.
    avail = {"$arb_spring_F": 45000.0, "$arb_spring_R": 25000.0,
             "$brakebias": 0.68, "$tirepressure_F": 20.0}
    diag = engineer.diagnose("understeer on entry", rep, avail)
    vmap = engineer.plan_to_vars(diag.get("plan", []), avail)
    check("race engineer softens front ARB for understeer",
          diag.get("ok") and any(
              it["var"] == "$arb_spring_F" and it["proposed"] < 45000
              for it in diag["plan"]),
          str([(it["var"], it.get("proposed")) for it in diag.get("plan", [])]))
    check("plan_to_vars emits only in-range $-vars",
          bool(vmap) and all(k.startswith("$") for k in vmap),
          str(vmap))
    try:
        os.remove(lapcsv)
    except OSError:
        pass

    print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
