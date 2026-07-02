"""SweepRunner: the overnight loop — apply, robot laps, measure, repeat.

One daemon thread walks the Strategy: for every proposed config it applies the
$vars (respawn), re-arms the game's AI driver (a respawn kills the vehicle-VM
AI state — learned live), runs the line-crossing lap session until enough
VALID laps or the per-config clock runs out, and scores the config by its
median valid lap time. Every eval is appended to a JSONL ledger as it happens,
so a crashed sweep loses nothing.

Safety rails, all learned the hard way this project:
* refuses to start unless connected, timing free, and a start line placeable;
* two consecutive failed configs (no valid laps / apply error) abort the sweep
  — a stuck car must not burn the night;
* whatever happens, the finally-block RESTORES the best known config (or the
  baseline), so the car is never left wearing a random experiment;
* everything it says in-game goes through best-effort toasts.
"""

from __future__ import annotations

import json
import os
import threading
import time

from ..errors import BeamNGError
from ..sim import lua, tuning
from .search import Eval, Strategy
from .space import build_space

AI_ON = ("ai.setMode('span') ai.setSpeedMode('limit') ai.setSpeed({speed:.1f}) "
         "ai.setAggression({aggr:.2f}) return 'ai on'")
AI_OFF = "ai.setMode('disabled') return 'ai off'"
#: Seconds for the robot to get rolling before we call the config failed.
ROLL_TIMEOUT_S = 45.0
ROLL_SPEED_MS = 3.0
#: Per-config lap budget: laps * this many seconds, plus slack.
LAP_TIME_ALLOWANCE_S = 180.0
CONFIG_SLACK_S = 90.0
CONSECUTIVE_FAILS_ABORT = 2


class SweepRunner:
    """One sweep at a time; owns nothing while idle (pitwall-style daemon)."""

    POLL_S = 2.0  # lap-status poll cadence (tests shrink this)

    def __init__(self, app) -> None:  # app: beamng_mcp.app.App (duck-typed)
        self.app = app
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._state: dict = {"state": "idle"}
        self._history: list[Eval] = []
        self._ledger_path: str | None = None
        app.sim.add_disconnect_hook(self.shutdown)

    # -- game-facing primitives (small, so tests can substitute them) ----------
    def _toast(self, msg: str) -> None:
        try:
            with self.app.sim.lock:
                self.app.sim.bng.ui.display_message(str(msg))
        except Exception:  # noqa: BLE001
            pass

    def _apply(self, vars_map: dict) -> None:
        tuning.set_tuning_vars(self.app.sim, vars_map)

    def _tuning_full(self) -> dict:
        return tuning.get_tuning_full(self.app.sim)

    def _robot(self, on: bool, speed_kmh: float, aggression: float) -> None:
        code = AI_ON.format(speed=speed_kmh / 3.6, aggr=aggression) if on else AI_OFF
        lua.vehicle_lua(self.app.sim, code)

    def _speed_ms(self) -> float:
        from ..sim import telemetry as telemetry_svc

        t = telemetry_svc.telemetry(self.app.sim)
        st = t.get("state") or {}
        vel = st.get("vel") or [0.0, 0.0, 0.0]
        v = sum((c or 0.0) ** 2 for c in vel) ** 0.5
        if v < 0.5:
            v = float((t.get("electrics") or {}).get("wheelspeed") or 0.0)
        return v

    # -- lifecycle ---------------------------------------------------------------
    def start(self, vars: list[str] | None = None, configs: int = 20,
              laps_per_config: int = 3, minutes: int = 120,
              speed_kmh: float = 110.0, aggression: float = 0.85,
              save_best_as: str | None = None, seed: int = 7) -> dict:
        if self._thread is not None and self._thread.is_alive():
            raise BeamNGError("a sweep is already running; stop_setup_sweep first")
        self.app.sim.require_connected()
        busy = self.app.timer.busy()
        if busy:
            raise BeamNGError(f"timing is busy ({busy}) — stop it before a sweep")

        full = self._tuning_full()
        space = build_space(full.get("vars") or {}, include=vars)
        if not space:
            raise BeamNGError("no sweepable levers on this car "
                              "(no rate/lsd/bias vars with usable ranges)")
        if self.app.timer.line is None:
            self.app.timer.set_start_line()  # park ON the circuit before starting

        strategy = Strategy(space=space, budget=max(2, configs), seed=seed)
        self._history = []
        stamp = int(time.time())
        os.makedirs(self.app.settings.logs_dir, exist_ok=True)
        self._ledger_path = os.path.join(self.app.settings.logs_dir,
                                         f"sweep_{stamp}.jsonl")
        self._state = {
            "state": "running", "eval": 0, "budget": strategy.budget,
            "space": [{"var": p.var, "lo": p.lo, "hi": p.hi, "start": p.start}
                      for p in space],
            "deadline": time.monotonic() + minutes * 60.0,
            "laps_per_config": max(1, laps_per_config),
            "speed_kmh": speed_kmh, "aggression": aggression,
            "save_best_as": save_best_as,
        }
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(strategy,),
                                        daemon=True)
        self._thread.start()
        self._toast(f"SETUP SWEEP: {strategy.budget} configs x "
                    f"{self._state['laps_per_config']} laps — the robot drives, "
                    "you watch (or sleep)")
        return {"state": "running", "budget": strategy.budget,
                "space": self._state["space"], "ledger": self._ledger_path,
                "note": ("robot laps from the start line at the car's position; "
                         "results land in the ledger as they happen")}

    def status(self) -> dict:
        scored = [e for e in self._history if e.objective is not None]
        best = min(scored, key=lambda e: e.objective) if scored else None
        baseline = self._history[0] if self._history else None
        out = {
            "state": self._state.get("state", "idle"),
            "eval": len(self._history),
            "budget": self._state.get("budget"),
            "best": ({"vars": best.vars, "lap_time_s": best.objective}
                     if best else None),
            "baseline_lap_time_s": baseline.objective if baseline else None,
            "ledger": self._ledger_path,
            "history": [{"vars": e.vars, "objective": e.objective}
                        for e in self._history],
        }
        if best and baseline and baseline.objective is not None:
            out["gain_s"] = round(baseline.objective - best.objective, 3)
        return out

    def stop(self) -> dict:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=30.0)
        self._thread = None
        return self.status()

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=10.0)
        self._thread = None
        if self._state.get("state") == "running":
            self._state["state"] = "stopped"

    # -- the sweep ---------------------------------------------------------------
    def _run(self, strategy: Strategy) -> None:
        consecutive_fails = 0
        aborted: str | None = None
        try:
            while not self._stop.is_set():
                if time.monotonic() > self._state["deadline"]:
                    aborted = "time budget reached"
                    break
                cand = strategy.propose(self._history)
                if cand is None:
                    break
                i = len(self._history) + 1
                self._state["eval"] = i
                self._toast(f"SWEEP {i}/{strategy.budget}: trying "
                            + self._fmt_vars(cand))
                objective, times = self._evaluate(cand)
                self._history.append(Eval(vars=cand, objective=objective))
                self._ledger({"eval": i, "vars": cand, "objective": objective,
                              "valid_times": times})
                if objective is None:
                    consecutive_fails += 1
                    self._toast(f"SWEEP {i}: no valid laps — "
                                f"{consecutive_fails}/{CONSECUTIVE_FAILS_ABORT} strikes")
                    if consecutive_fails >= CONSECUTIVE_FAILS_ABORT:
                        aborted = ("two consecutive configs produced no valid laps "
                                   "— car stuck or robot can't lap; aborting")
                        break
                else:
                    consecutive_fails = 0
                    best = strategy.best(self._history)
                    self._toast(f"SWEEP {i}: {self._fmt_time(objective)}"
                                + (f"  (best {self._fmt_time(best.objective)})"
                                   if best else ""))
        except Exception as exc:  # noqa: BLE001 — the thread must never leak
            aborted = f"internal error: {exc!r}"
        finally:
            self._finish(strategy, aborted)

    def _evaluate(self, cand: dict) -> tuple[float | None, list[float]]:
        """One config -> (median valid lap time | None, valid times)."""
        laps_needed = self._state["laps_per_config"]
        try:
            self._apply(cand)
        except Exception:  # noqa: BLE001 — a failed apply is a failed config
            return None, []
        try:
            self._robot(True, self._state["speed_kmh"], self._state["aggression"])
            t0 = time.monotonic()
            while time.monotonic() - t0 < ROLL_TIMEOUT_S:
                if self._stop.is_set():
                    return None, []
                if self._speed_ms() > ROLL_SPEED_MS:
                    break
                time.sleep(self.POLL_S)
            else:
                return None, []

            self.app.timer.start_lap_session(hz=30.0)
            valid: list[float] = []
            seen = 0
            deadline = (time.monotonic()
                        + laps_needed * LAP_TIME_ALLOWANCE_S + CONFIG_SLACK_S)
            while (len(valid) < laps_needed and time.monotonic() < deadline
                   and not self._stop.is_set()):
                time.sleep(self.POLL_S)
                st = self.app.timer.lap_session_status()
                if st.get("state") != "running":
                    break
                n = int(st.get("count") or 0)
                if n > seen:
                    seen = n
                    try:
                        lap = self.app.timer.last_lap()
                    except Exception:  # noqa: BLE001
                        continue
                    rep = lap.get("report") or {}
                    if rep.get("valid") and lap.get("lap_time_s"):
                        valid.append(float(lap["lap_time_s"]))
                    elif not valid and seen >= laps_needed + 2:
                        break  # lapping fine but every lap dirty — more waiting won't help
            if not valid:
                return None, []
            valid.sort()
            return valid[len(valid) // 2], valid
        finally:
            try:
                self.app.timer.stop_lap_session()
            except Exception:  # noqa: BLE001
                pass

    def _finish(self, strategy: Strategy, aborted: str | None) -> None:
        best = strategy.best(self._history)
        baseline = self._history[0] if self._history else None
        restore = best.vars if best else (baseline.vars if baseline else None)
        restored = False
        try:
            self._robot(False, 0.0, 0.0)
        except Exception:  # noqa: BLE001
            pass
        if restore:
            try:
                self._apply(restore)
                restored = True
                save_as = self._state.get("save_best_as")
                if save_as and best:
                    tuning.save_config(self.app.sim, save_as)
            except Exception:  # noqa: BLE001
                pass
        gain = None
        if best and baseline and baseline.objective is not None:
            gain = baseline.objective - best.objective
        summary = "SWEEP " + ("ABORTED: " + aborted if aborted else "done")
        if best:
            summary += (f" — best {self._fmt_time(best.objective)}"
                        + (f", {gain:+.2f} s vs baseline" if gain is not None else "")
                        + ("; best config applied" if restored else ""))
        self._toast(summary)
        self._state.update({
            "state": "aborted" if aborted else "done",
            "reason": aborted, "restored_best": restored,
            "gain_s": round(gain, 3) if gain is not None else None,
        })

    # -- helpers -------------------------------------------------------------------
    def _ledger(self, entry: dict) -> None:
        if not self._ledger_path:
            return
        try:
            with open(self._ledger_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError:
            pass

    @staticmethod
    def _fmt_time(s: float | None) -> str:
        if s is None:
            return "-"
        m = int(s // 60)
        return f"{m}:{s - m * 60:06.3f}"

    @staticmethod
    def _fmt_vars(vars_map: dict) -> str:
        return " ".join(f"{k}={v:g}" for k, v in list(vars_map.items())[:3]) + (
            " ..." if len(vars_map) > 3 else "")
