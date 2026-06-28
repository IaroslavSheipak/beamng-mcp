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
  config.py            # typed settings: paths, host/port, env overrides
  errors.py            # typed error envelope (the {ok:false,...} contract)
  sim/                 # THE PRECIOUS INTEGRATION LAYER — ported verbatim + tested
    connection.py      # BeamNGpy lifecycle + the single lock
    vehicle.py         # handle resolution, _use_current, eviction, classic sensors
    lua.py             # vehicle-VM Lua chunks + json contract
    telemetry.py       # rich poll + GE/OutGauge fallback
    tuning.py          # part config, $vars set/clamp, tire pressure, parts swap
    outgauge.py        # UDP parser (kept ~as-is)
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

0. **Scaffold** — `pyproject.toml`, `src/` package, ruff + pytest + mypy, move v1 to `legacy/` as the porting reference.
1. **Port `sim/` + `timing/`** with contract tests → reach `connect → telemetry → tune → lap` parity live.
2. **Redesign `analysis/`** against the recorded-lap fixtures (golden reports; validity + spike rejection prove out).
3. **Tool layer** — typed schemas + docs; parity check vs the v1 tool surface.
4. **Live re-validation** — drive laps; same checks as today's v1 live test.

## Definition of done (professional)

- pip-installable, pinned deps, entry point
- no module > ~400 lines; no god-class
- analysis never emits a metric it can't stand behind (validity/confidence surfaced)
- green suite incl. golden analysis fixtures
- live-validated parity on the core loop
