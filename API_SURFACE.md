# API_SURFACE.md — VERIFIED contract

Ground-truthed by installing into a Windows venv and introspecting the real
source with `inspect.signature` + reading the package source.

- **beamngpy**: `1.35.1`
- **mcp**: `1.28.1`
- **Python**: 3.12 (Windows venv)
- **site-packages**: `C:\Users\Iaroslav\beamng-mcp\.venv\Lib\site-packages`
- venv python (from WSL): `/mnt/c/Users/Iaroslav/beamng-mcp/.venv/Scripts/python.exe`
- beamngpy source dir: `...\site-packages\beamngpy`

Status: **VERIFIED** (signatures copied from live introspection).

---

## 1. beamngpy — imports

```python
from beamngpy import BeamNGpy, Scenario, Vehicle
from beamngpy.sensors import Electrics, State, Damage, Timer, GForces
# also exported from top-level beamngpy:
#   ScenarioObject, StaticObject, Level, MeshRoad, Road, vec3, angle_to_quat,
#   ProceduralBump, ProceduralCone, ... (procedural meshes)
```

## 2. BeamNGpy (core)

```python
BeamNGpy(host: str, port: int, home: str | None = None, binary: str | None = None,
         user: str | None = None, quit_on_close: bool = True, debug: bool | None = None,
         headless: bool = False, nogpu: bool = False, gfx: str | None = None)

BeamNGpy.open(self, extensions=None, *args, launch=True, debug=None,
              listen_ip='127.0.0.1', **opts) -> BeamNGpy
BeamNGpy.close(self) -> None
BeamNGpy.disconnect(self)
```

**IMPORTANT — scenario/vehicle/sim controls are NOT methods on BeamNGpy directly;
they live on namespaced sub-API objects set up in `_setup_api()`. Many are also
aliased onto the instance.** Key sub-APIs and aliases:

```python
bng.scenario  # ScenarioApi
bng.load_scenario   = bng.scenario.load
bng.start_scenario  = bng.scenario.start
bng.restart_scenario= bng.scenario.restart
bng.stop_scenario   = bng.scenario.stop
bng.get_current_scenario = bng.scenario.get_current

bng.control   # ControlApi
bng.step   = bng.control.step
bng.pause  = bng.control.pause
bng.resume = bng.control.resume

bng.vehicles  # VehiclesApi
bng.spawn_vehicle   = bng.vehicles.spawn
bng.despawn_vehicle = bng.vehicles.despawn

bng.traffic   # TrafficApi  (spawn_traffic/start_traffic/reset_traffic/stop_traffic)
bng.env       # EnvironmentApi (set_tod/set_weather_preset/set_gravity)
bng.camera bng.ui bng.debug bng.settings bng.system
```

Sub-API signatures (verified):

```python
ScenarioApi.load(self, scenario: Scenario, precompile_shaders=True,
                 connect_player_vehicle=True, connect_existing_vehicles=True) -> None
ScenarioApi.start(self, restrict_actions: bool | None = None) -> None
ScenarioApi.restart(self) -> None
ScenarioApi.stop(self) -> None
ScenarioApi.get_current(self, connect: bool = True) -> Scenario

VehiclesApi.spawn(self, vehicle: Vehicle, pos: Float3, rot_quat=(0,0,0,1),
                  cling=True, connect=True) -> bool

# bng.settings.set_deterministic(), bng.settings.set_steps_per_second(int)
```

Typical session: `bng = BeamNGpy('localhost', 25252, home=...)` → `bng.open()` →
build `Scenario`, `scenario.make(bng)` → `bng.scenario.load(scenario)` →
`bng.scenario.start()` → ... → `bng.close()`.

## 3. Scenario

```python
Scenario(level: str | Level, name: str, path: str | None = None,
         human_name: str | None = None, description: str | None = None,
         difficulty: int = 0, authors: str = 'BeamNGpy',
         restrict_actions: bool = False, **options)

Scenario.add_vehicle(self, vehicle: Vehicle, pos: Float3 = (0,0,0),
                     rot_quat: Quat = (0,0,0,1), cling: bool = True) -> None
Scenario.make(self, bng: BeamNGpy) -> None
```

## 4. Vehicle

```python
Vehicle(vid: str, model: str, port: int | None = None, license: str | None = None,
        color=None, color2=None, color3=None, extensions=None,
        part_config: str | None = None, **options)

Vehicle.connect(self, bng: BeamNGpy, tries: int = 5) -> None
Vehicle.disconnect(self) -> None
Vehicle.control(self, steering=None, throttle=None, brake=None, parkingbrake=None,
                clutch=None, gear=None, is_adas=False) -> None
Vehicle.set_shift_mode(self, mode: str) -> None
Vehicle.set_part_config(self, cfg: StrDict) -> None
Vehicle.get_part_config(self) -> StrDict
Vehicle.set_color(self, rgba=(1.0,1.0,1.0,1.0)) -> None
Vehicle.teleport(self, pos: Float3, rot_quat=None, reset=True) -> bool
Vehicle.recover(self) -> None
```

### Sensors on a Vehicle (namespaced sub-API, with instance aliases)

`vehicle.sensors` is a `Sensors` manager (NOT a dict). Aliases set in `__init__`:

```python
vehicle.attach_sensor = vehicle.sensors.attach
vehicle.detach_sensor = vehicle.sensors.detach
vehicle.poll_sensors  = vehicle.sensors.poll

Sensors.attach(self, name: str, sensor: Sensor) -> None
Sensors.detach(self, name: str) -> None
Sensors.poll(self, *sensor_names: str) -> None
Sensors.__getitem__(self, key: str) -> Sensor   # vehicle.sensors['electrics']
```

### AI

```python
vehicle.ai  # AIApi
AIApi.set_mode(self, mode: str) -> None          # 'span'/'random'/'manual'/'chase'/'disabled'
AIApi.set_speed(self, speed: float, mode='limit') -> None   # mode 'limit'|'set'
AIApi.set_target(self, target: str, mode='chase') -> None
AIApi.set_waypoint(self, waypoint: str) -> None
AIApi.set_aggression(self, aggr: float) -> None
AIApi.drive_in_lane(self, lane: bool) -> None
AIApi.set_script(self, script: list[dict], cling=True) -> None
# also: vehicle.ai_set_mode etc. aliases exist
```

## 5. Classic sensors module (`beamngpy.sensors`)

These are the polled, vehicle-attached classic sensors. **Each subclasses `dict`**
(`Electrics.__mro__ == [Electrics, Sensor, dict, object]`). Construct with **no args**:

```python
Electrics()   State()   Damage()   Timer()   GForces()
```

**Data access pattern**: attach → poll → read the sensor object **as a dict**.

```python
electrics = Electrics()
vehicle.attach_sensor('electrics', electrics)
# ... after vehicle.poll_sensors():
vehicle.poll_sensors()
rpm = electrics['rpm']                 # the sensor object itself is a dict
# equivalently:
rpm = vehicle.sensors['electrics']['rpm']
```

There is **no `.data` attribute** — the dict subclass holds the values directly
(populated via `Sensor.decode_response`). Use `[key]` / `.get(key)` / `.items()`.
State exposes e.g. `pos`, `dir`, `vel`, `rotation`; Damage exposes `damage`,
`part_damage`; Timer exposes `time`; GForces exposes `gx/gy/gz` etc.

## 6. MCP (FastMCP)

```python
from mcp.server.fastmcp import FastMCP   # VERIFIED import line

mcp = FastMCP(name: str | None = None, instructions: str | None = None, ...,
              host: str = '127.0.0.1', port: int = 8000, ...)

@mcp.tool(name=None, title=None, description=None, annotations=None,
          icons=None, meta=None, structured_output=None)
def my_tool(...): ...

@mcp.resource(uri: str, *, name=None, title=None, description=None, mime_type=None, ...)
@mcp.prompt(name=None, title=None, description=None, icons=None)

mcp.run(transport: Literal['stdio','sse','streamable-http'] = 'stdio',
        mount_path: str | None = None) -> None
```

Entrypoint pattern:

```python
if __name__ == '__main__':
    mcp.run()            # defaults to stdio transport
```
