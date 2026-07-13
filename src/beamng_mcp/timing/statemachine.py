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
import os
import threading
import time
from collections.abc import Callable
from statistics import median
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

#: Session-distance gate: once >=2 valid laps banked their distance, a closing
#: lap deviating from their median by more than this fraction is not a real lap
#: of this track (live failure: a respawn-truncated 857 m "lap" on a 1100 m
#: circuit read as valid and stole "best").
DISTANCE_TOL = 0.05
#: Idle guard: an open lap whose car stays below IDLE_SPEED_MS for this many
#: consecutive seconds is aborted (partial discarded, session stays armed) — a
#: driver parked in menus must not roll a phantom recording (live: 3:55 at 0 m).
IDLE_ABORT_S = 60.0
IDLE_SPEED_MS = 0.5
#: Line crossings are ignored for this long after the recorder (re)starts or the
#: session re-arms after an abort: a setup-apply respawn TELEPORTS the car, and a
#: teleport segment must never read as a crossing (live failure: "LAP 20" while
#: one physical lap was driven, off a dead recorder + respawn jumps).
CROSS_GRACE_S = 2.0


def fmt_time(s: float) -> str:
    """Format seconds as ``M:SS.mmm``."""
    m = int(s // 60)
    return f"{m}:{s - m * 60:06.3f}"


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
        self._logs_dir = logs_dir
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
        #: Set by abort_current_lap: the session worker re-arms + resumes recording.
        self._rearm = threading.Event()
        self._abort_reason: str | None = None
        self._tt: dict = {"state": "idle"}
        self._sess: dict = {"state": "idle"}
        self._laps: list = []
        sim.add_disconnect_hook(self.shutdown)

    # -- forensic event trail --------------------------------------------------
    def _log_event(self, msg: str) -> None:
        """One-line append to ``logs_dir/timer_events.log``. Two live debugging
        rounds died for lack of exactly this trail (which session started, what
        failed, what got aborted and why). Best-effort: never raises."""
        try:
            os.makedirs(self._logs_dir, exist_ok=True)
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
            with open(os.path.join(self._logs_dir, "timer_events.log"),
                      "a", encoding="utf-8") as fh:
                fh.write(f"{stamp} {msg}\n")
        except Exception:
            pass

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

    def _read_kin(self, vid: str) -> tuple[list | None, float]:
        """Cached player position + speed (the recorder keeps v.state fresh).
        Polls directly while the recorder is down (parked after an idle abort)
        so the car's wake-up is still seen."""
        with self.sim.lock:
            v = self.sim.vehicles.get(vid)
            if v is None:
                return None, 0.0
            if not self.recorder.running:
                try:
                    v.poll_sensors()
                except Exception:
                    return None, 0.0
            st = dict(v.state)
        vel = st.get("vel") or [0.0, 0.0, 0.0]
        return st.get("pos"), math.sqrt(sum((c or 0.0) ** 2 for c in vel))

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
        self._abort_reason = None
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
            res = self.recorder.start(self._poll_rich, hz=hz)
            if not res.get("ok"):  # never time a trial over a dead recorder
                self._tt = {"state": "error",
                            "error": f"recorder failed to start: {res.get('error')}"}
                self._draw_text("TIMER ERROR", sl_pos, _ORANGE)
                return
            self._tt = {"state": "running", "go_time": go}
            armed = False
            last_draw = 0.0
            prev = None
            cross_t = None
            timed_out = False
            idle_since: float | None = None
            while not self._stop.is_set():
                time.sleep(0.12)
                now = time.monotonic()
                el = now - go
                if el > 900:
                    timed_out = True
                    break
                pos, speed = self._read_kin(vid)
                if not pos:
                    continue
                if speed >= IDLE_SPEED_MS:
                    idle_since = None
                elif idle_since is None:
                    idle_since = now
                elif now - idle_since >= IDLE_ABORT_S:
                    self._abort_reason = (
                        f"parked below {IDLE_SPEED_MS:g} m/s for {IDLE_ABORT_S:.0f} s")
                    break
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
        if self._abort_reason:  # aborted (idle guard / abort_current_lap) — no lap
            discarded = self._discard_partial(stop.get("path"))
            reason = f"aborted: {self._abort_reason}"
            if discarded:
                reason += " — partial lap discarded"
            self._tt = {"state": "aborted", "armed": armed, "reason": reason,
                        "discarded": discarded}
            self._abort_reason = None
            self._draw_text("NO LAP", sl_pos, _ORANGE)
        elif cross_t is not None:
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
        self._sess = {"state": "running", "lap": 0, "t_cross": None, "best": None,
                      "distances": []}
        self._abort_reason = None
        self._rearm.clear()
        self._stop.clear()
        self._thread = threading.Thread(target=self._sess_run, args=(float(hz),), daemon=True)
        self._thread.start()
        self._log_event(f"lap session started (hz={float(hz):g}, vid={self._lap_vid})")
        return {"state": "running", "note": "auto-lap ON — just drive; each flying lap self-times"}

    def _start_recording(self, hz: float) -> bool:
        """Start the recorder and FAIL LOUD if it can't: a session must never
        keep timing laps over a dead recorder (live failure: laps ticked with
        zero CSVs behind them — nothing to analyze, nothing to prove)."""
        res = self.recorder.start(self._poll_rich, hz=hz)
        if res.get("ok"):
            return True
        self._fail_session(f"recorder failed to start: {res.get('error')}")
        return False

    def _fail_session(self, error: str) -> None:
        try:
            self.recorder.stop()
        except Exception:
            pass
        # the dead recorder's partial must not survive as a lap_*.csv
        self._discard_partial(self.recorder.path)
        self._sess = {**self._sess, "state": "error", "error": error}
        self._log_event(f"SESSION ERROR: {error}")
        if self.line is not None:
            self._draw_text("TIMER ERROR — session stopped", self.line.pos, _ORANGE)

    def _sess_run(self, hz: float) -> None:
        try:
            vid = self._lap_vid
            sl_pos = self.line.pos
            if not self._start_recording(hz):
                return
            armed = False
            prev = None
            idle_since: float | None = None
            parked = False  # recorder intentionally down, waiting for movement
            ignore_until = time.monotonic() + CROSS_GRACE_S
            while not self._stop.is_set():
                time.sleep(0.12)
                now = time.monotonic()
                if self._rearm.is_set():  # an abort discarded the open partial
                    self._rearm.clear()
                    armed, prev, idle_since = False, None, None
                    parked = True  # recorder is down until the resume branch
                    ignore_until = now + CROSS_GRACE_S
                pos, speed = self._read_kin(vid)
                if not pos:
                    continue
                if speed >= IDLE_SPEED_MS:
                    idle_since = None
                    if not self.recorder.running and parked:  # abort over — resume
                        if not self._start_recording(hz):
                            return
                        parked = False
                        armed, prev = False, None
                        ignore_until = now + CROSS_GRACE_S
                elif idle_since is None:
                    idle_since = now
                elif self.recorder.running and now - idle_since >= IDLE_ABORT_S:
                    self.abort_current_lap(
                        f"parked below {IDLE_SPEED_MS:g} m/s for {IDLE_ABORT_S:.0f} s")
                    continue
                if not self.recorder.running:
                    if parked:
                        continue  # parked: nothing recording, nothing to time
                    # Not parked and not recording = the recorder DIED mid-lap
                    # (poll error). Blind timing is worse than no timing.
                    err = self.recorder.status().get("poll_error") or "stopped unexpectedly"
                    self._fail_session(f"recorder died: {err}")
                    return
                if not armed and math.dist(pos, sl_pos) > 40.0:
                    armed = True
                cur = (now, pos)
                ct = (line_cross(prev, cur, self.line)
                      if armed and now >= ignore_until else None)
                prev = cur
                if ct is not None:
                    stop = self.recorder.stop()
                    self._close_lap(ct, stop)
                    if not self._start_recording(hz):
                        return
                    armed = False
                    prev = None
                    ignore_until = time.monotonic() + CROSS_GRACE_S
            self.recorder.stop()
            self._sess["state"] = "stopped"
        except Exception as exc:  # noqa: BLE001
            self._sess = {"state": "error", "error": repr(exc)}
            try:
                self.recorder.stop()
            except Exception:
                pass

    def _close_lap(self, ct: float, stop: dict) -> None:
        """Bookkeep one line crossing: register the closing lap (with the
        session-distance verdict) — or, on the session's first crossing, just
        open lap 1. Only session-valid laps may take "best". A crossing whose
        recording failed is DISCARDED, never registered — a lap without a CSV
        behind it cannot be analyzed, compared or believed."""
        t_cross = self._sess.get("t_cross")
        if t_cross is not None and not stop.get("path"):
            self._sess["discarded"] = self._sess.get("discarded", 0) + 1
            self._log_event(f"crossing discarded — no recording ({stop.get('error')})")
            self._draw_text("CROSSING DISCARDED — no recording", self.line.pos, _ORANGE)
            self._sess["t_cross"] = ct
            return
        if t_cross is not None:
            lt = round(ct - t_cross, 3)
            self._sess["lap"] += 1
            num = self._sess["lap"]
            dist = stop.get("distance_m")
            ok, reason = self._check_lap_distance(dist)
            lap = {"num": num, "time": lt, "csv": stop.get("path"),
                   "distance_m": dist, "valid": ok}
            if reason:
                lap["invalid_reason"] = reason
            self._laps.append(lap)
            best = self._sess.get("best")
            is_best = ok and (best is None or lt < best)
            if is_best:
                self._sess["best"] = lt
            tag = "  *BEST*" if is_best else ("" if ok else "  INVALID")
            color = _GREEN if is_best else (_YELLOW if ok else _ORANGE)
            self._log_event(
                f"LAP {num} {fmt_time(lt)} valid={ok}"
                + (f" dist={dist:.0f}m" if dist is not None else "")
                + (f" ({reason})" if reason else ""))
            self._draw_text(f"LAP {num}  {fmt_time(lt)}{tag}", self.line.pos, color)
        else:
            self._log_event("first crossing — lap 1 opened")
        self._sess["t_cross"] = ct

    def _check_lap_distance(self, dist: float | None) -> tuple[bool, str | None]:
        """Session-level distance gate: valid laps bank their distance; once >=2
        are banked, a closing lap deviating from their median by more than
        DISTANCE_TOL is invalid (respawn-/cut-truncated). Per-lap validity
        (analysis.validity) can't know this — it takes session knowledge."""
        if dist is None:
            return True, None
        pool: list = self._sess.setdefault("distances", [])
        if len(pool) >= 2:
            med = median(pool)
            if med > 0 and abs(dist - med) / med > DISTANCE_TOL:
                return False, f"distance {dist:.0f} m vs session median {med:.0f} m"
        pool.append(dist)
        return True, None

    def lap_session_status(self) -> dict:
        s = dict(self._sess)
        laps = []
        for x in self._laps:
            e: dict = {"num": x["num"], "time": fmt_time(x["time"]), "time_s": x["time"],
                       "valid": x.get("valid", True)}
            if x.get("distance_m") is not None:
                e["distance_m"] = x["distance_m"]
            if x.get("invalid_reason"):
                e["invalid_reason"] = x["invalid_reason"]
            laps.append(e)
        out: dict = {
            "state": s.get("state", "idle"),
            "count": len(self._laps),
            "laps": laps,
            "best": fmt_time(s["best"]) if s.get("best") is not None else None,
        }
        dists = s.get("distances") or []
        if len(dists) >= 2:  # the distance gate is armed — surface its yardstick
            out["median_distance_m"] = round(median(dists), 1)
        if s.get("discarded"):
            out["discarded_crossings"] = s["discarded"]
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
        if last.get("distance_m") is not None:
            out["distance_m"] = last["distance_m"]
        if last.get("csv") and self._analyze:
            try:
                out["report"] = self._analyze(last["csv"])
            except Exception as exc:  # noqa: BLE001
                out["analyze_error"] = repr(exc)
        if last.get("valid") is False:  # session-distance verdict overlays the report
            out["valid"] = False
            out["invalid_reason"] = last.get("invalid_reason")
            rep = out.get("report")
            if isinstance(rep, dict) and rep.get("ok"):
                rep["valid"] = False
                val = rep.setdefault("validity", {})
                val["valid"] = False
                val.setdefault("reasons", []).append(last.get("invalid_reason"))
        return out

    def stop_lap_session(self) -> dict:
        if self._sess.get("state") == "running":
            self._stop.set()
            if self._thread is not None:
                self._thread.join(timeout=4.0)
            self._log_event(f"lap session stopped ({len(self._laps)} laps)")
        return self.lap_session_status()

    # -- mid-session abort (setup apply / idle guard) -------------------------
    def _discard_partial(self, path: str | None) -> bool:
        """Delete an aborted partial CSV: it must never survive as a ``lap_*.csv``
        (``recent_laps``/``latest_lap`` glob those). Registered laps are kept."""
        if not path or any(x.get("csv") == path for x in self._laps):
            return False
        try:
            os.remove(path)
        except OSError:
            return False
        return True

    def abort_current_lap(self, reason: str) -> dict:
        """Close the open recording WITHOUT registering a lap; discard its CSV.

        Called before a respawn (``apply_setup``) and by the idle guard. A lap
        session (the pit session wraps the same timer) stays armed: ``t_cross``
        clears and the worker re-arms, so the next line crossing opens a clean
        lap. A time trial cannot survive its car respawning/parking, so it is
        cancelled. No-op when nothing is open.
        """
        owner = self.busy()
        self._log_event(f"abort_current_lap ({owner or 'no-op'}): {reason}")
        if owner == "lap_session":
            stop = self.recorder.stop()
            discarded = self._discard_partial(stop.get("path"))
            self._sess["t_cross"] = None
            self._rearm.set()
            return {"aborted": True, "mode": owner, "session": "re-armed",
                    "discarded": discarded, "reason": reason}
        if owner == "time_trial":
            self._abort_reason = reason
            self._stop.set()
            t = self._thread
            if t is not None and t is not threading.current_thread():
                t.join(timeout=4.0)
            return {"aborted": True, "mode": owner, "session": "idle",
                    "discarded": bool(self._tt.get("discarded")), "reason": reason}
        if owner == "lap":
            stop = self.recorder.stop()
            discarded = self._discard_partial(stop.get("path"))
            return {"aborted": True, "mode": owner, "session": "idle",
                    "discarded": discarded, "reason": reason}
        return {"aborted": False, "mode": None, "session": "idle",
                "discarded": False, "reason": reason}

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
