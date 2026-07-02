"""PitWallSession — the resident in-game pit board (the tier-1 cockpit fix).

A chat window is the wrong medium for a live driving loop: the driver would
have to alt-tab mid-session to read anything. This daemon closes that gap by
pushing every lap's verdict INTO the game as UI toasts
(``bng.ui.display_message`` — first-class beamngpy 1.35.1 API, acked). One MCP
call starts it; the driver just drives. Chat is only needed at decision points
(apply a setup change, save a build) — and Mara's toast says so.

It wraps the existing :class:`LapTimer` lap-session mode (interpolated
line-crossing auto-timing, one recorder by construction) and simply WATCHES
``lap_session_status`` for new laps from a daemon thread. Each new lap:

* lap time toast (with *BEST* when it is one),
* the honesty line — an invalid lap says so and is never coached,
* Mara's current P1 call (telemetry-only race engineer). ADVISORY ONLY —
  applying respawns the car, so it must stay a deliberate chat decision.

Construction is side-effect-free (no thread, no sockets) and the daemon stops
with the connection via a disconnect hook, mirroring the App wiring style.
"""

from __future__ import annotations

import threading
import time

from .errors import BeamNGError


class PitWallSession:
    """One watcher thread over the LapTimer's session mode + in-game toasts."""

    #: Seconds between lap-status polls (tests shrink this).
    POLL_S = 2.0

    def __init__(self, app) -> None:  # app: beamng_mcp.app.App (duck-typed)
        self.app = app
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._state: dict = {"state": "idle"}
        self._last_read: dict = {}
        app.sim.add_disconnect_hook(self.shutdown)

    # -- in-game output (best-effort; a UI hiccup must never kill the session) --
    def toast(self, msg: str) -> None:
        try:
            with self.app.sim.lock:
                self.app.sim.bng.ui.display_message(str(msg))
        except Exception:  # noqa: BLE001 — display is best-effort by contract
            pass

    # -- lifecycle -------------------------------------------------------------
    def start(self, hz: float = 30.0) -> dict:
        if self._thread is not None and self._thread.is_alive():
            raise BeamNGError("pit session already running; stop_pit_session first")
        self.app.sim.require_connected()
        session = self.app.timer.start_lap_session(hz=hz)  # raises if timing is busy
        self._stop.clear()
        self._state = {"state": "running", "laps_read": 0}
        self._last_read = {}
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.toast("MARA ON THE WALL — start/finish set at your car; every flying lap self-times")
        self.toast("Just drive. Lap verdicts appear here; setup changes stay in chat.")
        return {
            "state": "running",
            "session": session,
            "note": ("pit board is LIVE in-game: lap time + balance read + Mara's call "
                     "after every lap; no chat needed while driving"),
        }

    def status(self) -> dict:
        timer_status = self.app.timer.lap_session_status()
        return {
            "state": self._state.get("state", "idle"),
            "laps_read": self._state.get("laps_read", 0),
            "last_read": self._last_read or None,
            "timing": timer_status,
        }

    def stop(self) -> dict:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=4.0)
        self._thread = None
        timing = self.app.timer.stop_lap_session()
        self._state["state"] = "stopped"
        best = timing.get("best")
        self.toast(f"PIT SESSION OVER — {timing.get('count', 0)} lap(s)"
                   + (f", best {best}" if best else ""))
        return {"state": "stopped", "laps_read": self._state.get("laps_read", 0),
                "timing": timing, "last_read": self._last_read or None}

    def shutdown(self) -> None:
        """Disconnect hook: stop the watcher; the timer's own hook stops timing."""
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=4.0)
        self._thread = None
        if self._state.get("state") == "running":
            self._state["state"] = "stopped"

    # -- the watcher -----------------------------------------------------------
    def _run(self) -> None:
        seen = 0
        while not self._stop.is_set():
            time.sleep(self.POLL_S)
            try:
                st = self.app.timer.lap_session_status()
            except Exception:  # noqa: BLE001 — transient read; keep watching
                continue
            if st.get("state") != "running":
                break  # someone stopped the underlying session (or it errored)
            count = int(st.get("count") or 0)
            if count > seen:
                seen = count
                self._read_lap(count, st)
                self._state["laps_read"] = seen
        if self._state.get("state") == "running":
            self._state["state"] = "stopped"

    def _read_lap(self, n: int, session_status: dict) -> None:
        """One completed lap -> the pit-board read (never raises)."""
        try:
            lap = self.app.timer.last_lap()
        except Exception as exc:  # noqa: BLE001
            self.toast(f"PIT: lap {n} could not be read ({exc})")
            return
        rep = lap.get("report") or {}
        lap_time = lap.get("lap_time")
        is_best = lap.get("lap_time_s") is not None and (
            session_status.get("best") == lap_time or n == 1)
        read: dict = {"lap": n, "lap_time": lap_time, "valid": rep.get("valid")}

        self.toast(f"LAP {n}  {lap_time}" + ("   *BEST*" if is_best else ""))

        if not rep:
            self._last_read = read
            return
        if not rep.get("valid"):
            reasons = "; ".join((rep.get("validity") or {}).get("reasons") or [])
            self.toast(f"invalid lap ({reasons}) — not coaching off this one")
            read["reasons"] = reasons
            self._last_read = read
            return

        bal = rep.get("balance") or {}
        tendency = bal.get("tendency") or "unknown"
        idx = bal.get("understeer_index")
        grip = (rep.get("grip") or {}).get("envelope_g")
        line = f"read: {tendency}"
        if idx is not None:
            line += f" ({idx:+.2f})"
        if grip is not None:
            line += f" | grip {grip:.2f} g"
        self.toast(line)
        read.update({"tendency": tendency, "understeer_index": idx, "grip_envelope_g": grip})

        try:
            eng = self.app.race_engineer("", lap_path=lap.get("csv"))
            plan = (eng.get("diagnosis") or {}).get("plan") or []
        except Exception:  # noqa: BLE001 — advice is a bonus, never a blocker
            plan = []
        if plan:
            top = plan[0]
            call = (f"MARA: {top.get('lever')} {top.get('var')} "
                    f"{top.get('current'):g} -> {top.get('proposed'):g} "
                    f"[{top.get('confidence')}] — say 'apply' in chat")
            self.toast(call)
            read["mara_p1"] = {k: top.get(k) for k in
                               ("lever", "var", "current", "proposed", "confidence")}
            read["plan_items"] = len(plan)
        self._last_read = read
