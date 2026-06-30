"""LapTimer: the SINGLE owner of lap timing.

v1 had three entrypoints (start_lap / start_time_trial / start_lap_session) that
each drove one shared RichLapRecorder + one 3D-text id behind independent guards,
so running two at once silently corrupted each other's CSVs. Here they are one
state machine with one ``mode``; ``busy()`` makes them mutually exclusive by
construction. Detection uses the interpolated plane-crossing from ``line`` (not a
proximity sphere); the recorder keeps ``v.state`` fresh so the worker just reads
cached positions under the lock.

Service layer: methods return data / status dicts and raise BeamNGError on a soft
failure (busy, no line); the tool layer envelopes. The ``analyze`` callable is
injected at wiring time so timing doesn't depend on the analysis package.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from typing import Protocol

from ..errors import BeamNGError
from ..sim.context import Simulator
from ..sim.vehicle import use_current
from .line import GATE_HALF, StartLine, gate_endpoints, line_cross
from .recorder import RichLapRecorder


class MotionSource(Protocol):
    """Duck-typed MotionSim listener -- keeps ``timing`` decoupled from ``sim``
    (the same reason ``analyze`` is injected rather than imported)."""

    def latest(self) -> dict | None: ...

# GForces -> analysis convention (gx=longitudinal, gy=lateral, gz vertical ~+1g),
# in g-units. Ported from v1. (Phase 2: MotionSim supersedes this — gravity-
# excluded accel + true yaw rate, no axis-swap / sign guessing.)
G = 9.80665
GF_LONG_SIGN = 1.0
GF_LAT_SIGN = 1.0
GF_VERT_SIGN = -1.0

_AMBER = (1.0, 0.7, 0.05, 1.0)
_GREEN = (0.1, 1.0, 0.2, 1.0)
_YELLOW = (1.0, 1.0, 0.2, 1.0)
_ORANGE = (1.0, 0.5, 0.1, 1.0)


def fmt_time(s: float) -> str:
    """Format seconds as ``M:SS.mmm``."""
    m = int(s // 60)
    return "%d:%06.3f" % (m, s - m * 60)


def _num(x: object) -> object:
    try:
        return float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return x


def _motion_fields(motion: MotionSource | None) -> dict:
    """``{ms_yaw_rate, ms_ax, ms_ay, ms_az}`` from the latest MotionSim packet.

    ``{}`` if there is no listener or no fresh packet (no-op-safe: the recorder
    just leaves those CSV columns blank, e.g. when MotionSim isn't enabled
    in-game). Duck-typed on ``.latest()`` so this is testable without a real
    :class:`~beamng_mcp.sim.motionsim.MotionSimListener`.
    """
    if motion is None:
        return {}
    pkt = motion.latest()
    if pkt is None:
        return {}
    ax, ay, az = pkt["acc"]
    return {"ms_yaw_rate": pkt["ang_vel"][2], "ms_ax": ax, "ms_ay": ay, "ms_az": az}


class LapTimer:
    """One recorder, one gate, one worker — three mutually exclusive modes."""

    def __init__(
        self,
        sim: Simulator,
        logs_dir: str,
        analyze: Callable[[str], dict] | None = None,
        motion: MotionSource | None = None,
    ) -> None:
        self.sim = sim
        self.recorder = RichLapRecorder(logs_dir)
        self._analyze = analyze
        #: Optional MotionSimListener (duck-typed) -- adds true yaw rate +
        #: gravity-excluded accel columns to the rich recorder when present.
        self._motion = motion
        self.line: StartLine | None = None
        self._text_id: int | None = None
        self._lap_vid: str | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._tt: dict = {"state": "idle"}
        self._sess: dict = {"state": "idle"}
        self._laps: list = []
        sim.add_disconnect_hook(self.shutdown)

    # -- mutual exclusion ----------------------------------------------------
    def busy(self) -> str | None:
        """Which mode currently owns the subsystem, or None."""
        if self._tt.get("state") in ("counting", "running"):
            return "time_trial"
        if self._sess.get("state") == "running":
            return "lap_session"
        if self.recorder.running:
            return "lap"
        return None

    def _require_free(self, for_what: str) -> None:
        owner = self.busy()
        if owner:
            raise BeamNGError(f"timing busy ({owner}); stop it before {for_what}")

    # -- the recorder poll source (player car -> one RICH row) ---------------
    def _poll_rich(self) -> dict:
        sim = self.sim
        with sim.lock:
            vid = self._lap_vid
            if vid is None or vid not in sim.vehicles:
                vid = use_current(sim, None)
                self._lap_vid = vid
            v = sim.vehicles[vid]
            v.poll_sensors()
            e = dict(v.sensors["electrics"])
            gf = dict(v.sensors["gforces"])
            st = dict(v.state)
        pos = st.get("pos") or [None, None, None]
        vel = st.get("vel") or [0.0, 0.0, 0.0]
        d = st.get("dir") or [1.0, 0.0, 0.0]
        speed = math.sqrt(sum((c or 0.0) ** 2 for c in vel))
        heading = math.atan2(d[1] or 0.0, d[0] or 0.0)
        bgx, bgy, bgz = gf.get("gx") or 0.0, gf.get("gy") or 0.0, gf.get("gz") or 0.0

        def ch(*names: str) -> object:
            for k in names:
                if e.get(k) is not None:
                    return _num(e[k])
            return None

        row = {
            "speed": speed,
            "posx": pos[0], "posy": pos[1], "posz": pos[2],
            "heading": heading,
            "gx": GF_LONG_SIGN * bgy / G,
            "gy": GF_LAT_SIGN * bgx / G,
            "gz": GF_VERT_SIGN * bgz / G,
            "rpm": ch("rpm"), "gear": ch("gear_index", "gear"),
            "throttle": ch("throttle"), "brake": ch("brake"),
            "brakeF": ch("brakeF"), "brakeR": ch("brakeR"),
            "steering": ch("steering"), "steering_input": ch("steering_input"),
            "clutch": ch("clutch"), "boost": ch("boost", "turboBoost"),
            "wheelspeed": ch("wheelspeed"),
            "abs_active": ch("abs_active"), "tcs_active": ch("tcs_active"),
            "esc_active": ch("esc_active"),
        }
        row.update(_motion_fields(self._motion))
        return row

    def _read_pos(self, vid: str) -> list | None:
        """Cached player position (the recorder keeps v.state fresh)."""
        with self.sim.lock:
            v = self.sim.vehicles.get(vid)
            return dict(v.state).get("pos") if v is not None else None

    # -- gate + text drawing (best-effort; never raises) ---------------------
    def _draw_text(self, text: str, pos: object, color: tuple = _YELLOW) -> None:
        try:
            with self.sim.lock:
                if self._text_id is not None:
                    try:
                        self.sim.bng.debug.remove_text(self._text_id)
                    except Exception:
                        pass
                self._text_id = self.sim.bng.debug.add_text(
                    [float(pos[0]), float(pos[1]), float(pos[2])], str(text),
                    color, cling=True, offset=2.0)
        except Exception:
            pass

    def _clear_text(self) -> None:
        try:
            with self.sim.lock:
                if self._text_id is not None:
                    self.sim.bng.debug.remove_text(self._text_id)
                    self._text_id = None
        except Exception:
            pass

    def _wipe_gates(self) -> None:
        """Brute-remove debug ids 0..63 (the GE handler tolerates missing ids), so
        even gates orphaned by stale post-reconnect ids are cleared."""
        if not self.sim.bng:
            return
        rng = list(range(64))
        try:
            self.sim.bng.debug.remove_spheres(rng)
        except Exception:
            pass
        for i in rng:
            for remove in (self.sim.bng.debug.remove_polyline, self.sim.bng.debug.remove_text):
                try:
                    remove(i)
                except Exception:
                    pass

    def _draw_gate(self, line: StartLine) -> None:
        a, b = gate_endpoints(line, GATE_HALF)
        with self.sim.lock:
            for draw in (
                lambda: self.sim.bng.debug.add_polyline([a, b], _GREEN, cling=True),
                lambda: self.sim.bng.debug.add_spheres(
                    [a, b], [0.6, 0.6], [_GREEN, _GREEN], cling=True, offset=0.6),
                lambda: self.sim.bng.debug.add_text(
                    list(line.pos), "START / FINISH", _GREEN, cling=True, offset=1.5),
            ):
                try:
                    draw()
                except Exception:
                    pass

    # -- start/finish line ---------------------------------------------------
    def set_start_line(self) -> dict:
        """Mark the car's current position + heading as the start/finish line."""
        with self.sim.lock:
            vid = use_current(self.sim, None)
            v = self.sim.vehicles[vid]
            v.poll_sensors()
            st = dict(v.state)
            pos = st.get("pos")
            heading = st.get("dir") or [1.0, 0.0, 0.0]
            if not pos:
                raise BeamNGError("could not read car position")
            self._wipe_gates()
        line = StartLine(pos=list(pos), heading=list(heading))
        self._draw_gate(line)
        self.line = line
        return {"pos": line.pos, "note": "green gate + START/FINISH label drawn"}

    def clear_gates(self) -> dict:
        with self.sim.lock:
            self._wipe_gates()
            self.line = None
            self._text_id = None
        return {"note": "all start/finish gates + timer text cleared"}

    # -- manual lap ----------------------------------------------------------
    def start_lap(self, hz: float = 30.0) -> dict:
        self._require_free("a lap recording")
        with self.sim.lock:
            self._lap_vid = use_current(self.sim, None)  # prime so poll 1 is fast
        return self.recorder.start(self._poll_rich, hz=hz)

    def stop_lap(self) -> dict:
        owner = self.busy()
        if owner in ("time_trial", "lap_session"):
            raise BeamNGError(f"the recorder is owned by {owner}; use stop_{owner} instead")
        res = self.recorder.stop()
        if res.get("ok") and res.get("path") and self._analyze:
            try:
                res["report"] = self._analyze(res["path"])
            except Exception as exc:  # noqa: BLE001
                res["analyze_error"] = repr(exc)
        return res

    def lap_status(self) -> dict:
        return self.recorder.status()

    # -- time trial ----------------------------------------------------------
    def start_time_trial(self, countdown: int = 3, hz: float = 30.0) -> dict:
        self._require_free("a time trial")
        if not self.line:
            self.set_start_line()
        self._tt = {"state": "counting"}
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._tt_run, args=(int(countdown), float(hz)), daemon=True)
        self._thread.start()
        return {"state": "counting", "note": "watch the in-game countdown; drive on GO"}

    def _tt_run(self, countdown: int, hz: float) -> None:
        try:
            with self.sim.lock:
                vid = use_current(self.sim, None)
            self._lap_vid = vid
            sl_pos = self.line.pos
            for i in range(max(0, countdown), 0, -1):
                if self._stop.is_set():
                    self._clear_text()
                    self._tt = {"state": "cancelled"}
                    return
                self._draw_text(str(i), sl_pos, _AMBER)
                time.sleep(1.0)
            self._draw_text("GO!", sl_pos, _GREEN)
            go = time.monotonic()
            self.recorder.start(self._poll_rich, hz=hz)
            self._tt = {"state": "running", "go_time": go}
            armed = False
            last_draw = 0.0
            prev = None
            cross_t = None
            timed_out = False
            while not self._stop.is_set():
                time.sleep(0.12)
                now = time.monotonic()
                el = now - go
                if el > 900:
                    timed_out = True
                    break
                pos = self._read_pos(vid)
                if not pos:
                    continue
                if now - last_draw > 0.33:
                    self._draw_text(fmt_time(el), pos, _YELLOW)
                    last_draw = now
                dist = math.dist(pos, sl_pos)
                if not armed and dist > 40.0:
                    armed = True
                cur = (now, pos)
                if armed:
                    ct = line_cross(prev, cur, self.line)
                    if ct is not None:
                        cross_t = ct
                        break
                prev = cur
            stop = self.recorder.stop()
            self._finish_trial(cross_t, timed_out, go, armed, stop, sl_pos)
        except Exception as exc:  # noqa: BLE001
            self._tt = {"state": "error", "error": repr(exc)}
            try:
                self.recorder.stop()
            except Exception:
                pass

    def _finish_trial(self, cross_t, timed_out, go, armed, stop, sl_pos) -> None:
        if cross_t is not None:
            lt = cross_t - go
            self._tt = {"state": "done", "lap_time": round(lt, 3), "auto": True,
                        "armed": armed, "csv": stop.get("path")}
            self._draw_text(f"LAP  {fmt_time(lt)}", sl_pos, _GREEN)
        elif self._stop.is_set() and not timed_out:
            lt = time.monotonic() - go
            self._tt = {"state": "done", "lap_time": round(lt, 3), "auto": False,
                        "armed": armed, "csv": stop.get("path"),
                        "note": "manual stop — time at stop, not a line crossing"}
            self._draw_text(f"LAP  {fmt_time(lt)}", sl_pos, _GREEN)
        else:
            self._tt = {"state": "aborted", "armed": armed,
                        "reason": "timed out (900 s) without crossing the line",
                        "csv": stop.get("path")}
            self._draw_text("NO LAP", sl_pos, _ORANGE)

    def time_trial_status(self) -> dict:
        tt = dict(self._tt)
        out: dict = {"state": tt.get("state", "idle"), "line_set": self.line is not None}
        if tt.get("state") == "running" and tt.get("go_time"):
            el = time.monotonic() - tt["go_time"]
            out["elapsed_s"] = round(el, 2)
            out["elapsed"] = fmt_time(el)
        if tt.get("lap_time") is not None:
            out["lap_time_s"] = tt["lap_time"]
            out["lap_time"] = fmt_time(tt["lap_time"])
            out["auto"] = tt.get("auto")
            out["csv"] = tt.get("csv")
        if tt.get("error"):
            out["error"] = tt["error"]
        if tt.get("reason"):
            out["reason"] = tt["reason"]
        return out

    def stop_time_trial(self) -> dict:
        if self._tt.get("state") in ("counting", "running"):
            self._stop.set()
            if self._thread is not None:
                self._thread.join(timeout=4.0)
        return self.time_trial_status()

    # -- hands-off lap session ----------------------------------------------
    def start_lap_session(self, hz: float = 30.0) -> dict:
        self._require_free("a lap session")
        if not self.line:
            self.set_start_line()
        with self.sim.lock:
            self._lap_vid = use_current(self.sim, None)
        self._laps = []
        self._sess = {"state": "running", "lap": 0, "t_cross": None, "best": None}
        self._stop.clear()
        self._thread = threading.Thread(target=self._sess_run, args=(float(hz),), daemon=True)
        self._thread.start()
        return {"state": "running", "note": "auto-lap ON — just drive; each flying lap self-times"}

    def _sess_run(self, hz: float) -> None:
        try:
            vid = self._lap_vid
            sl_pos = self.line.pos
            self.recorder.start(self._poll_rich, hz=hz)
            armed = False
            prev = None
            while not self._stop.is_set():
                time.sleep(0.12)
                now = time.monotonic()
                pos = self._read_pos(vid)
                if not pos:
                    continue
                if not armed and math.dist(pos, sl_pos) > 40.0:
                    armed = True
                cur = (now, pos)
                ct = line_cross(prev, cur, self.line) if armed else None
                prev = cur
                if ct is not None:
                    stop = self.recorder.stop()
                    t_cross = self._sess.get("t_cross")
                    if t_cross is not None:
                        lt = round(ct - t_cross, 3)
                        self._sess["lap"] += 1
                        num = self._sess["lap"]
                        self._laps.append({"num": num, "time": lt, "csv": stop.get("path")})
                        best = self._sess.get("best")
                        is_best = best is None or lt < best
                        if is_best:
                            self._sess["best"] = lt
                        self._draw_text(
                            f"LAP {num}  {fmt_time(lt)}{'  *BEST*' if is_best else ''}",
                            sl_pos, _GREEN if is_best else _YELLOW)
                    self._sess["t_cross"] = ct
                    self.recorder.start(self._poll_rich, hz=hz)
                    armed = False
                    prev = None
            self.recorder.stop()
            self._sess["state"] = "stopped"
        except Exception as exc:  # noqa: BLE001
            self._sess = {"state": "error", "error": repr(exc)}
            try:
                self.recorder.stop()
            except Exception:
                pass

    def lap_session_status(self) -> dict:
        s = dict(self._sess)
        out: dict = {
            "state": s.get("state", "idle"),
            "count": len(self._laps),
            "laps": [{"num": x["num"], "time": fmt_time(x["time"]), "time_s": x["time"]}
                     for x in self._laps],
            "best": fmt_time(s["best"]) if s.get("best") is not None else None,
        }
        if s.get("state") == "running" and s.get("t_cross"):
            out["current_lap_elapsed"] = fmt_time(time.monotonic() - s["t_cross"])
        if s.get("error"):
            out["error"] = s["error"]
        return out

    def last_lap(self) -> dict:
        if not self._laps:
            raise BeamNGError("no completed laps yet — cross the start/finish line once")
        last = self._laps[-1]
        out = {"num": last["num"], "lap_time": fmt_time(last["time"]),
               "lap_time_s": last["time"], "csv": last.get("csv")}
        if last.get("csv") and self._analyze:
            try:
                out["report"] = self._analyze(last["csv"])
            except Exception as exc:  # noqa: BLE001
                out["analyze_error"] = repr(exc)
        return out

    def stop_lap_session(self) -> dict:
        if self._sess.get("state") == "running":
            self._stop.set()
            if self._thread is not None:
                self._thread.join(timeout=4.0)
        return self.lap_session_status()

    # -- teardown (registered as a disconnect hook) --------------------------
    def shutdown(self) -> None:
        """Stop any worker + the recorder. Runs BEFORE sim.disconnect takes the lock
        (the workers take sim.lock each loop, so joining under it would deadlock)."""
        self._stop.set()
        try:
            self.recorder.stop()
        except Exception:
            pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=4.0)
        self._thread = None
        self._tt = {"state": "idle"}
        self._sess = {"state": "idle"}
