# beamng-mcp v2 — Rebuild Design

Branched off `feat/ai-race-engineer` (which carries the 8 verified bug fixes).

## Why rebuild, and what kind

v1 works and its **BeamNGpy integration is verified-correct** — but it carries a
1448-line god-class (`session.py`), an analysis layer whose metrics don't hold up
(balance index pinned ~`+1.0`; grip envelope poisoned by impact spikes — both seen
live), no packaging, and thin tests.

**Principle: port-and-verify, do NOT blank-page.** Every undocumented integration
fact moves over from v1 *verbatim* with a citation + a contract test:

- the `techCore.openServer(25252)` socket dance (consumer build, no `.tech` license)
- the vehicle-VM Lua chunks: `$vars` via `v.data.variables`, the wheels probe, the
  `drivingDynamics/CMU` traction-control toggle, and their `jsonEncode` contract
- attach semantics: `open(launch=False)` + `quit_on_close=launch`, `disconnect()` not `close()`
- `_use_current` handle resolution + priming-retry + the eviction fix
- the per-vehicle "wedge" failure mode and recovery
- GForces axis/sign/units, OutGauge UDP format, the path/port config

The expensive, invisible knowledge is preserved. Only the **structure** and the
**analysis math** are redesigned.

## Target architecture (proper package)

```
src/beamng_mcp/
  server.py            # FastMCP app: thin tool defs -> services
  app.py               # wires Simulator + LapTimer + MotionSimListener + DriveLogger;
                        # race_engineer()/apply_setup() orchestration
  config.py            # typed settings: paths, host/port, env overrides
  errors.py            # typed error envelope (the {ok:false,...} contract)
  sim/                 # THE PRECIOUS INTEGRATION LAYER — ported verbatim + tested
    connection.py      # BeamNGpy lifecycle + the single lock (context.py)
    vehicle.py         # handle resolution, _use_current, eviction, classic sensors
    lua.py             # vehicle-VM Lua chunks + json contract
    telemetry.py       # rich poll + GE/OutGauge fallback
    tuning.py          # part config, $vars set/clamp, tire pressure, parts swap
    outgauge.py        # UDP parser (kept ~as-is)
    motionsim.py       # BNG1 UDP parser + background listener (NEW capability)
    scenario.py        # ACTIVE-mode spawn()/run_test() (v1 tool-surface parity)
    drivelog.py        # plain OutGauge-only drive logger + summary (v1 logger.py port)
  timing/              # bounded context: ONE recorder owner by design
    recorder.py        # RichLapRecorder (kept)
    line.py            # start/finish geometry + _line_cross (the fixed one)
    statemachine.py    # lap / time-trial / session as ONE machine, not 3 entrypoints
  analysis/            # THE REDESIGN
    model.py           # typed Lap / Sample / Corner
    ingest.py          # CSV -> validated samples
    validity.py        # NEW: reject laps with a full stop / sub-threshold distance
    cleaning.py        # NEW: impact-spike rejection (a wall hit must not set the envelope)
    grip.py            # friction circle + real envelope (post-cleaning)
    balance.py         # REWORKED: a metric that works, or honest null + reason
    braking.py, ride.py
    report.py          # typed report carrying validity + confidence flags
  engineer/            # tuning advisor (structure kept)
    knowledge.py       # symptom->lever matrix + selftests
    advisor.py         # plan generation, clamp vs live ranges
tests/
  fixtures/            # the REAL lap CSVs already recorded (clean lap 3, crash laps 4/5/6)
  test_line_cross.py   # geometry unit tests (port from the v1 fix)
  test_lua_contract.py # every Lua chunk's JSON shape
  test_analysis.py     # golden reports on fixtures (validity + spike rejection)
  test_smoke.py        # offline tool registration + engineer selftests
```

## Keep / Restructure / Redesign

| | What |
|---|---|
| **KEEP** (port + test) | `sim/*`, `timing/recorder` + `timing/line`, `engineer/knowledge`, OutGauge parser, config/paths |
| **RESTRUCTURE** | god-class → bounded contexts; 3 timing entrypoints → 1 state machine; opaque `dict` tool params → typed schemas; typed error envelope |
| **REDESIGN** | lap-validity gating, impact-spike rejection, a balance metric that's trustworthy (or null), typed report with confidence flags |

## Phases — each ends GREEN (tested + live-checked)

0. **Scaffold** ✅ — `pyproject.toml`, `src/` package, ruff + pytest + mypy, move v1 to `legacy/` as the porting reference.
1. **Port `sim/` + `timing/`** ✅ with contract tests → reach `connect → telemetry → tune → lap` parity live.
2. **Redesign `analysis/`** ✅ against the recorded-lap fixtures (golden reports; validity + spike rejection prove out).
3. **Tool layer** ✅ — `app.py` wires one `Simulator` + one `LapTimer` (analysis injected) + one `MotionSimListener`
   (started/stopped with the connection) + one `DriveLogger`. `server.py` is the FastMCP tool layer: 43 tools, full
   v1 parity — including `spawn`/`run_test` (new `sim/scenario.py`) and the plain OutGauge drive logger (new
   `sim/drivelog.py`), neither of which the original phase breakdown above called out as their own modules.
   `timing/recorder.py`'s `RICH_FIELDS` now carries `ms_yaw_rate`/`ms_ax`/`ms_ay`/`ms_az` from the MotionSim
   listener. 125 tests green (101 + 24 new); ruff/mypy clean on every file this phase touched (pre-existing
   findings in Phase 0–2 code were left as-is — out of this phase's scope).
4. **Live re-validation** ✅ — drive laps through v2 against the running game; verify a real MotionSim `BNG1`
   packet end to end. Done live 2026-06-30 (v0.38.6, etk800/sunburst2/bastion): `connect`/`status`/
   `current_vehicles`/`telemetry`/`get_tuning_full`/`wheel_telemetry`/`car_mass`/`outgauge`/`disconnect` all
   verified against the running game; a 20 s / 577-sample / ~100 m drive through `start_lap`→`stop_lap` produced
   a report with **every** `ms_yaw_rate`/`ms_ax`/`ms_ay`/`ms_az` cell populated (motion listener kept pace at
   30 Hz) and — proving the Phase 2 redesign live, not just on fixtures — `valid: false` (sub-200 m, correctly
   rejected, not silently treated as a clean lap) and `balance.tendency: "unknown"` / `understeer_index: null`
   (honest non-calibration on ordinary driving, not v1's pinned `+1.0`). MotionSim velocity magnitude matched
   OutGauge's speed independently (~25 km/h vs 26.2 km/h), confirming the `BNG1` parse (endianness + field
   layout) against ground truth.

   **Live finding (not a v2 bug, but blocked verification until fixed):** BeamNG's own Protocols UI can persist
   `protocols_motionSim_maxUpdateRate` as the **string** `"j"` (a stray keystroke) instead of a number into
   `<userpath>/settings/settings.json`. `lua/vehicle/protocols.lua:152` does `1 / protocol.updateRate` with no
   type guard, which is a *fatal* (non-coercible-string) Lua exception that disables **every** vehicle spawn
   game-wide — looks exactly like file corruption ("целостность файлов нарушена" / "error loading vehicle") and
   survives a full game reinstall, since it's a per-profile *setting*, not a game file. Fix: edit
   `settings.json`, set `protocols_motionSim_maxUpdateRate` to a real number (BeamNG's own default is `60`, see
   `lua/vehicle/protocols/motionSim.lua:16`). Numeric-*looking* strings (e.g. a port saved as `"4445"`) are fine
   — Lua/LuaJIT auto-coerces those for both arithmetic and `string.format("%d", ...)`; only a genuinely
   non-numeric string (`"j"`) is fatal. Also: MotionSim's port defaults to the **same** port as OutGauge (4444)
   if its UI field is left blank — set it explicitly (we used 4445) or the two protocols collide on one socket.
   **Also confirmed:** repeated fresh `connect()`s across many short-lived processes (as in this verification
   session) reproduces the documented per-vehicle "wedge" (`KeyError('result')`/`VehicleUnavailable`) — same
   fix as always (restart BeamNG, reopen the tech socket). For a real session, reuse ONE long-lived `App()` /
   MCP server process rather than one-shot scripts.

## v2.1 — the user-friendliness layer (post-rebuild)

On top of the rebuilt core: `compare_laps` (baseline-vs-candidate deltas +
verdict — closes the tune → re-drive → confirm loop), `lap_coach` (driver-side
technique tips, the driver/car split made explicit), `doctor` (one-call
diagnostics encoding the Phase-4 live findings: the non-numeric
`protocols_*` settings corruption, the MotionSim/OutGauge port collision, the
beamngpy↔game version pairing), `lap_status`, and three guided-workflow MCP
prompts (`first_time_setup`, `pit_wall_session`, `track_day_debrief`).
47 tools; 149 tests. Legacy v1 root modules removed (git history keeps them).

## Definition of done (professional)

- pip-installable, pinned deps, entry point ✅
- no module > ~400 lines; no god-class ✅
- analysis never emits a metric it can't stand behind (validity/confidence surfaced) ✅ (live-proven above)
- green suite incl. golden analysis fixtures ✅ (125 tests)
- live-validated parity on the core loop ✅ (2026-06-30, see Phase 4 above)
