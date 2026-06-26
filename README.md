# beamng-mcp — MCP server for BeamNG.drive (Steam consumer build)

An [MCP](https://modelcontextprotocol.io) server that drives a stock, retail
**BeamNG.drive** install (Steam, v0.38.6.0) over BeamNGpy — **no BeamNG.tech
license required**. Because the consumer build exposes only the *classic* polled
CPU sensors, telemetry is limited to **Electrics / State / Damage / Timer /
GForces** plus the license-free **OutGauge UDP** dashboard stream. All of the
BeamNG.tech "automation" sensors (Camera, Lidar, Radar, Ultrasonic, AdvancedIMU,
Mesh, Powertrain, GPS, RoadsSensor, IdealRadar) are unavailable on this build and
are deliberately not used anywhere in this server.

> Status: **live-tested on the Steam build** (build 23007233 / v0.38.6).
> `connect` + `current_vehicles` + `telemetry` are confirmed working end to end
> (126 Electrics channels). The model is **passive**: you launch and play the
> game; the server sits idle and only attaches when you ask (e.g. "change car
> config"), operating on the car you're currently driving. It never launches the
> game, loads a scenario, or drives — unless you explicitly use the ACTIVE-mode
> tools (`spawn`, `set_control`, `run_test`).

## Install

```bat
cd C:\Users\Iaroslav\beamng-mcp
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Paths

| Name | Value |
| --- | --- |
| `GAME_HOME` (install, has `Bin64`) | `C:\Program Files (x86)\Steam\steamapps\common\BeamNG.drive` |
| `USERFOLDER` (active user data) | `C:\Users\Iaroslav\AppData\Local\BeamNG\BeamNG.drive\current` |
| `USER_VEHICLES` (user `.pc` configs) | `…\current\vehicles` |
| `INSTALL_VEHICLES` (stock zips, read-only) | `GAME_HOME\content\vehicles` |
| Integration host / port | `127.0.0.1` / `25252` |

Override any of these with environment variables before launch:
`BEAMNG_HOME`, `BEAMNG_USER`, `BEAMNG_HOST`, `BEAMNG_PORT`. The server runs under
Windows `python.exe`, so all paths are Windows-style (do not translate to
`/mnt/c`). Do **not** use `C:\Users\Iaroslav\Documents\BeamNG.drive` — that is a
stale settings-only stub.

## Per-session setup: open the integration socket

BeamNG's Steam **launcher strips custom flags**, so Steam launch options like
`-tcom` do **not** work (confirmed: the engine's command line drops them). Instead,
open the socket from the in-game console — verified working on the consumer build,
and it keeps your normal Steam session intact:

1. Launch BeamNG.drive (Steam) and get into a vehicle.
2. Press **`` ` ``/`~`** (tilde, top-left under Esc) to open the Lua console.
3. Run:

```lua
extensions.load('tech/techCore'); tech_techCore.openServer(25252)
```

You'll see `Started listening on 127.0.0.1/25252`. The socket stays open until you
close the game, and Claude can attach on demand. No license is needed —
`openServer` has no license gate (only tech-only sensors do, which this server
never uses).

**Alternative (no per-session typing):** launch the engine directly via a shortcut/
`.bat` to `Bin64\BeamNG.drive.x64.exe -tcom -tport 25252 -gfx dx11`. That opens the
socket automatically but bypasses the Steam launcher.

## Use it (passive workflow)

1. Launch BeamNG.drive (Steam) and drive normally — any car, any level.
2. When you want a change, ask Claude (e.g. *"claude, change car config"*,
   *"what's my engine doing?"*, *"give it more boost"*). Claude then:
   - `connect()` — attaches to your running game (no takeover),
   - `current_vehicles()` / `telemetry()` — finds and reads the car you're in,
   - `get_config()` → modify → `set_config()` — applies the change in place
     (BeamNG respawns your car with the new parts, like the in-game menu),
   - `disconnect()` — drops the socket; **your game keeps running**.

Nothing happens until you ask. `spawn` / `set_control` / `run_test` are ACTIVE
mode and only run if you explicitly request a fresh car, driving, or a test.

## Enable OutGauge (for `outgauge_telemetry`)

In **BeamNG.drive**: `Options > Other > Protocols` → enable **"OutGauge UDP
protocol"**; set **IP `127.0.0.1`**, **Port `4444`**; leave the **OutGauge ID
blank** (this yields 92-byte packets; a non-blank ID yields 96-byte packets,
which the parser also handles). A restart is not required but is recommended.

## Run / register with Claude

The MCP child process is a **Windows** `python.exe`, so it only understands
**Windows** paths. Even when you register from WSL, the *script argument* must be a
Windows path (WSL interop translates the interpreter binary path, but passes
arguments through literally — a `/mnt/c/...` script arg fails with
"can't open file ... No such file or directory").

Register from a **Windows** Claude Code shell (simplest, all paths native):

```bat
claude mcp add beamng-mcp -- C:\Users\Iaroslav\beamng-mcp\.venv\Scripts\python.exe C:\Users\Iaroslav\beamng-mcp\server.py
```

Or register from **WSL**, passing the script as a Windows path (note the `.exe`
interpreter is auto-translated, but the script arg is given Windows-style):

```bash
claude mcp add beamng-mcp -- \
  /mnt/c/Users/Iaroslav/beamng-mcp/.venv/Scripts/python.exe \
  "$(wslpath -w /mnt/c/Users/Iaroslav/beamng-mcp/server.py)"
```

`wslpath -w` yields `C:\Users\Iaroslav\beamng-mcp\server.py`. `BEAMNG_HOME`,
`BEAMNG_USER`, and all `.pc` paths are likewise Windows-only.

The server speaks MCP over **stdio**. You can also launch it directly on Windows:

```bat
C:\Users\Iaroslav\beamng-mcp\.venv\Scripts\python.exe C:\Users\Iaroslav\beamng-mcp\server.py
```

Run the offline self-check (no game needed):

```bat
C:\Users\Iaroslav\beamng-mcp\.venv\Scripts\python.exe smoke_test.py
```

## Tools

| Tool | Mode | Notes |
| --- | --- | --- |
| `connect` | attach | default `launch=False`: attaches to your running game; leaves it running on disconnect |
| `disconnect` | attach | drops the socket; does **not** close your game |
| `status` | offline | connection state |
| `current_vehicles` | attach | lists in-game vehicles, flags the player's car (live-tested) |
| `telemetry` | attach | 126 Electrics channels + Damage/GForces + State (current car); **falls back to GE-state + OutGauge** if the per-vehicle socket is down after a respawn |
| `get_config` | attach | compact config of your current car: installed parts + tuning vars (GE-side, robust) |
| `set_config` | attach | apply parts to your current car (respawns it in place) |
| `list_vehicle_models` | offline | install zips + user dirs + stock fallback |
| `list_configs` | offline | scans USER_VEHICLES for `*.pc` |
| `read_pc` / `write_pc` | offline | `.pc` JSON, confined to USER_VEHICLES |
| `outgauge_telemetry` | offline parser | needs OutGauge enabled in-game |
| `spawn` / `set_control` / `run_test` | ACTIVE | only when you explicitly ask Claude to spawn / drive / test |
| `start_logging` / `stop_logging` | drive log | record OutGauge to a CSV in `logs/`; `stop` returns a run summary (robust — no per-vehicle socket) |
| `summarize_drive` | offline | summarize a recorded drive (distance, top/avg speed, 0–100, gear usage, speed trace) |
| `vehicle_lua` | analysis | run a Lua chunk on the current car and return its value — deep introspection (powertrain, suspension, beams) for "analyze & improve" |

Every tool returns a JSON object `{"ok": bool, ...}` and never raises across the
MCP boundary. Tools that need the game return
`{"ok": false, "error": "not connected; call connect first"}` (or a clear
per-vehicle error) instead of a traceback.

### `.pc` config confinement (security)

`read_pc` / `write_pc` operate **only** inside `USER_VEHICLES`. Names containing
`/`, `\`, `:`, `..`, path separators, or characters outside `[A-Za-z0-9 _-.]`
are rejected, and the resolved real path is checked with `commonpath` to defeat
symlink / `..` escapes. The `.pc` extension is forced automatically.

## Limitations

- **No BeamNG.tech sensors.** Camera / Lidar / Radar / Ultrasonic / AdvancedIMU /
  Mesh / Powertrain / GPS / RoadsSensor / IdealRadar are license-gated and return
  nothing on this build; they are not exposed.
- **`set_config` respawns and repairs** the car (resets damage) — this is a
  beamngpy/engine side-effect of changing parts, same as the in-game parts menu.
- **Steam auto-updates** can break the beamngpy↔game protocol (you'll see a
  version-handshake error on connect). If a game update lands, re-pin `beamngpy`
  to the version matching the new game minor (e.g. game `0.38` → `beamngpy==1.35.x`)
  and reconnect.
- Core attach tools (`connect` / `current_vehicles` / `telemetry`) are
  **live-tested** on the Steam build. `set_config` apply and the ACTIVE-mode tools
  (`spawn` / `set_control` / `run_test`) are implemented against the verified API
  but exercise less-common paths — sanity-check them the first time you use them.

## Troubleshooting

- **`WinError 10061` / `ConnectionRefusedError` on connect** — BeamNG.drive is not
  running, is on a different port, or the integration port is blocked by Windows
  Firewall. Start the game (or `connect(launch=True)`), confirm port `25252`, and
  allow BeamNG.drive through the firewall, then retry.
- **How `connect(launch=True)` actually works (no tech license needed)** —
  beamngpy starts its **own** instance of `Bin64\BeamNG.drive.x64.exe` with
  `-nosteam -tcom -tport 25252` (verified in `beamngpy/beamng/beamng.py`,
  `_prepare_call`). The integration server is enabled by the **`-tcom`** flag plus
  the `tech\` Lua layer that already ships inside your retail install — there is
  **no license file, no `BeamNGpy.zip` mod, and no `researchHelper.txt`** involved
  (those are pre-1.x artifacts and do not exist in beamngpy 1.35.1). Consequence:
  do **not** pre-launch the game through Steam and expect `connect(launch=False)`
  to find it. Either let `launch=True` start the instance, or start the game
  yourself with `-tcom -tport 25252` first and then call `connect(launch=False)`.
- **Connect hangs / times out on first launch** — the game can take 30–60s to
  reach the point where the `-tcom` socket accepts connections; beamngpy waits and
  retries internally. If it ultimately fails, confirm no other instance is already
  bound to port 25252.
- **`outgauge_telemetry` always `received: false`** — OutGauge is not enabled, or
  the IP/port don't match the game's protocol settings (see "Enable OutGauge").
