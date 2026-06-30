"""ACTIVE-mode scenario control: spawn a vehicle, run an AI test drive.

Everything else in ``sim/`` operates on the car the user is ALREADY driving
(the passive, attach-on-demand model). ``spawn``/``run_test`` are the explicit
exception -- they create/load a scenario and drive a car with the BeamNGpy AI,
so the tool layer must only reach them when the user explicitly asks for a
test car or test run. Ported verbatim from v1 ``session.py`` (``spawn``,
``run_test``), with one fix: elapsed-time sampling uses ``time.monotonic``
(immune to NTP/DST steps) instead of v1's ``time.time()``.
"""

from __future__ import annotations

import time

from beamngpy import Scenario, Vehicle

from . import pc_config
from .context import Simulator
from .vehicle import attach_classic_sensors


def _resolve_config(model: str, config: str | None) -> str | None:
    """A named ``.pc`` config -> its confined path for ``Vehicle(part_config=...)``.

    Raises :class:`ValueError` if ``config`` doesn't resolve to a real, confined
    file (the same validation ``read_pc`` already does).
    """
    if not config:
        return None
    res = pc_config.read_pc(model, config)
    if not res.get("ok"):
        raise ValueError(res.get("error", f"config not found: {config}"))
    return pc_config.confined_target(model, config)


def spawn(
    sim: Simulator,
    model: str,
    config: str | None = None,
    vid: str = "ego",
    pos: tuple[float, float, float] = (0, 0, 0),
    rot_quat: tuple[float, float, float, float] = (0, 0, 0, 1),
    level: str = "gridmap_v2",
) -> dict:
    """Spawn a NEW vehicle, creating/loading a scenario if none is active yet."""
    sim.require_connected()
    with sim.lock:
        config_path = _resolve_config(model, config)
        v = Vehicle(vid, model=model, part_config=config_path)
        if sim.scenario is None:
            sc = Scenario(level, "mcp_session")
            sc.add_vehicle(v, pos=tuple(pos), rot_quat=tuple(rot_quat))
            sc.make(sim.bng)
            sim.bng.scenario.load(sc)
            sim.bng.scenario.start()
            sim.scenario = sc
        else:
            sim.bng.vehicles.spawn(v, tuple(pos), rot_quat=tuple(rot_quat))
        sim.sensors[vid] = attach_classic_sensors(v)
        sim.vehicles[vid] = v
        return {"vid": vid, "model": model, "config": config_path, "level": level}


def run_test(
    sim: Simulator,
    vid: str = "ego",
    model: str = "etk800",
    level: str = "west_coast_usa",
    ai_mode: str = "span",
    speed_kmh: float = 60.0,
    duration_s: float = 10.0,
    sample_hz: float = 5.0,
) -> dict:
    """Spawn-if-needed, drive with the BeamNGpy AI for ``duration_s``, sample
    telemetry, return a summary. The lock is released during each sample's
    ``time.sleep``, so a long run does not monopolize the whole session."""
    sim.require_connected()
    if vid not in sim.vehicles:
        spawn(sim, model=model, vid=vid, level=level)
    v = sim.vehicles[vid]
    samples: list[dict] = []
    try:
        with sim.lock:
            v.ai.set_mode(ai_mode)
            v.ai.set_speed(speed_kmh / 3.6, mode="limit")

        interval = 1.0 / sample_hz if sample_hz > 0 else 0.2
        n = max(1, int(round(duration_s * sample_hz)))
        t0 = time.monotonic()
        for _ in range(n):
            with sim.lock:
                v.poll_sensors()
                e = v.sensors["electrics"]
                st = dict(v.state)
            samples.append({
                "t": round(time.monotonic() - t0, 3),
                "speed": e.get("wheelspeed"),
                "rpm": e.get("rpm"),
                "pos": st.get("pos"),
            })
            time.sleep(interval)
        with sim.lock:
            v.ai.set_mode("disabled")
    except Exception:
        try:
            with sim.lock:
                v.ai.set_mode("disabled")
        except Exception:
            pass
        raise

    speeds = [s["speed"] for s in samples if s["speed"] is not None]
    positions = [s["pos"] for s in samples if s["pos"]]
    distance = 0.0
    for a, b in zip(positions, positions[1:], strict=False):
        distance += sum((b[i] - a[i]) ** 2 for i in range(3)) ** 0.5
    final_damage = None
    try:
        with sim.lock:
            final_damage = dict(v.sensors["damage"]).get("damage")
    except Exception:
        pass
    summary = {
        "max_speed": max(speeds) if speeds else None,
        "avg_speed": (sum(speeds) / len(speeds)) if speeds else None,
        "distance": distance,
        "final_damage": final_damage,
    }
    return {"vid": vid, "samples": samples, "summary": summary}
