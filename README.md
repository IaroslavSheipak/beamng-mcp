# beamng-mcp — your AI race engineer for BeamNG.drive

An [MCP](https://modelcontextprotocol.io) server that turns Claude (or any MCP
client) into a **pit-wall race engineer** for a stock, retail **BeamNG.drive**
install (Steam) — **no BeamNG.tech license required**. You drive; the AI times
your laps, analyzes the telemetry, coaches your driving, diagnoses the car's
balance from how you *say* it feels, and applies real setup changes to the
car's actual tuning sliders.

```text
you:    "the car pushes on entry and the rear snaps on throttle"
Mara:   copy — reading entry understeer, exit oversteer.
        First move, soften the front anti-roll bar ($arb_spring_F 45000 -> 39600).
        One change at a time. Re-drive it and I'll prove it either way.
you:    *drives*
Mara:   candidate FASTER by 0.84 s — carrying +2.1 km/h through the matched
        corners; balance moved to neutral. Keeping it. Want it saved as a .pc?
```

## What you can ask for

| You say | What happens |
| --- | --- |
| *"set up lap timing here"* | `set_start_line` draws a 3D gate; `start_lap_session` auto-times every flying lap |
| *"how were my laps?"* | `lap_session_status` / `analyze_lap` — times + grip, balance, braking, per-corner report |
| *"coach me"* | `lap_coach` — driver-side tips: braking effort vs the car's proven grip, coasting, under-driven corners |
| *"the car understeers, fix it"* | `race_engineer` — your words + lap telemetry + the car's real `$vars` → a ranked setup plan |
| *"apply it"* | `apply_setup` — clamped to the car's live ranges; respawns like the in-game Apply |
| *"did that help?"* | `compare_laps` — baseline vs candidate: lap time, corner speeds, balance shift, verdict |
| *"something's broken"* | `doctor` — checks paths, the game socket, protocol settings (incl. a corruption that mimics broken game files), ports |

Three **guided workflows** ship as MCP prompts (slash commands in Claude):
`/first_time_setup` (get connected, step by step), `/pit_wall_session` (the
full drive → feel → tune → prove loop), `/track_day_debrief` (analyze + coach
+ compare what you just drove).

## Quickstart

**1. Install** (Windows python — the server talks to a Windows game):

```bat
cd C:\Users\Iaroslav\beamng-mcp
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
```

**2. Register with Claude Code.** The console entry point keeps paths simple:

```bat
claude mcp add beamng -- C:\Users\Iaroslav\beamng-mcp\.venv\Scripts\beamng-mcp.exe
```

(From WSL the same registration works — the `.exe` path is what matters. Any
`.pc`/path arguments this server handles are **Windows** paths; never
translate them to `/mnt/c/...`.)

**3. Open the game's socket** (once per game session). BeamNG's Steam launcher
strips custom flags, so launch options don't work — use the in-game console:

1. Launch BeamNG.drive (Steam) and get into a vehicle.
2. Press **`` ` ``/`~`** (tilde) to open the Lua console and run:

```lua
extensions.load('tech/techCore'); tech_techCore.openServer(25252)
```

You'll see `Started listening on 127.0.0.1/25252`. The socket stays open until
the game closes. (Alternative, no typing: a shortcut to
`Bin64\BeamNG.drive.x64.exe -tcom -tport 25252` opens it automatically but
bypasses Steam.)

**4. Enable the telemetry protocols** (once, in-game, `Options > Others >
Protocols`):

- **OutGauge UDP**: IP `127.0.0.1`, port `4444`, ID blank → drive logging.
- **MotionSim** (optional but recommended): IP `127.0.0.1`, port **`4445`**
  (not blank — blank collides with OutGauge!), update rate `60` → laps get
  true yaw rate + gravity-excluded accel.

**5. Check it**: ask Claude to run `doctor` — every failed check comes with
its exact fix. Then just drive and talk.

The model is **passive**: you launch and play the game; the server attaches
only when you ask and detaching leaves the game running. It never drives or
spawns anything unless you explicitly use the ACTIVE-mode tools
(`spawn`, `set_control`, `run_test`).

## Paths / configuration

| Env var | Default | Meaning |
| --- | --- | --- |
| `BEAMNG_HOME` | `C:\Program Files (x86)\Steam\steamapps\common\BeamNG.drive` | install dir (has `Bin64`) |
| `BEAMNG_USER` | `C:\Users\Iaroslav\AppData\Local\BeamNG\BeamNG.drive\current` | active user profile (`.pc` configs live in `vehicles\`) |
| `BEAMNG_HOST` / `BEAMNG_PORT` | `127.0.0.1` / `25252` | the tech socket |
| `BEAMNG_LOGS_DIR` | `<cwd>\logs` | where lap/drive CSVs land |

## The race-engineer loop

1. **Time laps.** Three flavors, one shared recorder (mutually exclusive by
   construction): `start_lap`/`stop_lap` (manual), `start_time_trial`
   (3-2-1-GO countdown, one auto-timed lap), `start_lap_session` (hands-off —
   every flying lap self-times). Laps close on a true interpolated
   **line crossing**, not a proximity radius, so hairpins and pit lanes don't
   false-trigger. Every lap records ~30 Hz rich telemetry to a CSV and
   auto-analyzes.
2. **Read the lap.** The report carries **validity gating** (a crash/stopped
   lap is flagged, never silently compared to a hot lap), an impact-cleaned
   **grip envelope** (a wall hit can't inflate it), a **self-calibrated
   balance index** (honest `null` when the lap can't calibrate it), braking,
   ride, and per-corner minimum speeds.
3. **Tell Mara how it felt.** `race_engineer("understeer on entry, loose on
   throttle")` merges your words with the telemetry symptoms (agreement boosts
   confidence), reads the car's **real** tunable surface (`get_tuning_full` —
   same list as the in-game Tuning menu, with live min/max), and returns a
   ranked plan: which slider, which way, how much, why.
4. **Apply and prove.** `apply_setup(plan)` (respawns, like the in-game
   Apply); `set_tire_pressure` is live, no respawn. Re-drive, then
   `compare_laps` gives the verdict: lap-time delta, matched-corner speeds,
   balance shift. `save_config` persists the build as a `.pc` the in-game
   config menu can load.

What it tunes (resolved per-car at runtime, never hardcoded): springs,
anti-roll bars, bump & rebound damping, ride height, camber/toe/caster, brake
bias, differential, tire pressure — whatever the installed parts actually
expose. `list_parts`/`swap_parts` handle fitment-aware part changes (e.g. the
etk800 needs Rally/Race coilovers before spring/damper sliders exist).

## Tools (47)

**Connection & health** — `connect`, `disconnect`, `reconnect`, `status`,
`doctor`

**Your car, live** — `current_vehicles`, `telemetry`, `wheel_telemetry`,
`car_mass`, `outgauge_telemetry`, `vehicle_lua` (advanced Lua introspection)

**Lap timing** — `set_start_line`, `clear_gates`, `start_lap`, `lap_status`,
`stop_lap`, `start_time_trial`, `time_trial_status`, `stop_time_trial`,
`start_lap_session`, `lap_session_status`, `last_lap`, `stop_lap_session`

**Analysis & coaching** — `analyze_lap`, `compare_laps`, `lap_coach`,
`summarize_drive`, `start_logging`, `stop_logging`

**Race engineer & tuning** — `race_engineer`, `get_tuning_full`, `set_tuning`,
`apply_setup`, `set_tire_pressure`, `set_traction_control` (live A/B),
`get_config`, `set_config`, `list_parts`, `swap_parts`, `save_config`

**Configs & content** — `list_vehicle_models`, `list_configs`, `read_pc`,
`write_pc` (confined to the user vehicles folder; path-escape hardened)

**ACTIVE mode** (only when you explicitly ask) — `spawn`, `set_control`,
`run_test`

Every tool returns `{"ok": bool, ...}` and never raises across the MCP
boundary — failures come back as a clear message plus the usual fix.

## Honest limits (consumer build)

- **No BeamNG.tech sensors.** Camera/Lidar/Radar/IMU etc. are license-gated
  and deliberately unused. Telemetry is the classic polled set
  (Electrics/State/Damage/GForces) + license-free OutGauge/MotionSim UDP.
- The **balance index** uses the normalized steering channel, so it's a
  relative/trend metric (excellent for before/after deltas, not absolute
  degrees) — and it reports `null` rather than a number it can't stand behind.
- No suspension-travel channel: ride/bottoming is a **gz proxy** (flagged as
  such). Geometry vars (camber/toe/caster) are unitless multipliers with
  sometimes-reversed jbeam ranges — those plan items are flagged
  low-confidence.
- **Applying parts/tuning respawns the car** (repairs it, resets pose) — an
  engine-side effect, same as the in-game parts menu.
- **Steam auto-updates** can break the beamngpy↔game handshake: re-pin
  `beamngpy` to the matching minor (game `0.38.x` ↔ `beamngpy==1.35.x`).
  `doctor` reminds you of the pairing.

## Troubleshooting

Run `doctor` first — it automates most of this table.

- **`WinError 10061` / connection refused** — the game isn't running or the
  tech socket was never opened this session (see Quickstart step 3), or a
  firewall blocks port 25252.
- **Every vehicle spawn fails game-wide, looks like corrupted files, survives
  reinstall** — a known settings corruption: BeamNG's Protocols UI can persist
  a stray keystroke (e.g. `"j"`) into a numeric `protocols_*` setting in
  `<user>\settings\settings.json`; the game does arithmetic on it with no type
  guard. `doctor` detects it; fix by setting the key to a number (update-rate
  default is `60`) and restarting.
- **`connect(launch=True)`** starts the server's **own** game instance with
  `-nosteam -tcom -tport 25252` (no license, no mods involved). Don't
  pre-launch via Steam and expect `launch=False` to find it — open the socket
  in-game first (step 3).
- **First launch hangs** — the game needs 30–60 s before the socket accepts;
  beamngpy retries internally.
- **`outgauge_telemetry` says `received: false`** — OutGauge isn't enabled, or
  IP/port mismatch, or you're in a menu (the game only streams in-vehicle).
- **Per-vehicle tools wedge after many reconnects** (`KeyError('result')`) —
  a game-side per-vehicle socket wedge; restart BeamNG.drive and reopen the
  socket. Prefer one long-lived server session over repeated short-lived
  connects.

## Development

```bat
.venv\Scripts\python.exe -m pytest -q     & REM 149 tests, offline, ~7 s
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m mypy
```

Layout: `src/beamng_mcp/` — `server.py` (FastMCP tool layer + prompts),
`app.py` (service wiring), `sim/` (the verified BeamNGpy integration layer:
connection, vehicle handles, Lua contracts, tuning, OutGauge/MotionSim UDP,
doctor), `timing/` (one lap-timing state machine + rich recorder + line
geometry), `analysis/` (validity-gated, impact-cleaned lap metrics; compare;
coach), `engineer/` (the symptom→lever knowledge base + advisor). Design
history and the porting contract live in `REBUILD.md`.
