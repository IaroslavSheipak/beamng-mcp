"""App: wires the long-lived service singletons together.

``server.py``'s tools are thin wrappers over an :class:`App` instance: one
:class:`Simulator`, one :class:`LapTimer` (the analysis callback injected here
so ``timing`` never imports ``analysis``), one :class:`DriveLogger`, and one
:class:`MotionSimListener`. Construction has no side effects (no sockets, no
threads) so tests can build their own ``App()`` instead of sharing process-
global state; the motion listener starts/stops with the BeamNGpy connection via
:meth:`App.connect`/:meth:`App.disconnect`, since it only matters while a lap
might be recorded.

Also carries the two cross-context orchestrations that don't belong to any one
bounded context: :meth:`race_engineer` (tuning + analysis + engineer) and
:meth:`apply_setup` (engineer + tuning + pc_config). Ported from v1
``Session.race_engineer``/``Session.apply_setup``.
"""

from __future__ import annotations

from .analysis.report import analyze_lap
from .config import SETTINGS, Settings
from .engineer import advisor
from .errors import BeamNGError
from .optimizer.runner import SweepRunner
from .pitwall import PitWallSession
from .sim import drivelog, tuning
from .sim.context import Simulator
from .sim.motionsim import MotionSimListener
from .sim.raceline import RacingLineDrawer
from .timing.recorder import latest_lap
from .timing.statemachine import LapTimer


class App:
    """One Simulator + one LapTimer + one DriveLogger + one MotionSim listener."""

    def __init__(self, settings: Settings = SETTINGS) -> None:
        self.settings = settings
        self.sim = Simulator(settings)
        self.motion = MotionSimListener()
        self.timer = LapTimer(self.sim, settings.logs_dir, analyze=analyze_lap, motion=self.motion)
        self.drivelog = drivelog.DriveLogger(settings.logs_dir)
        self.pitwall = PitWallSession(self)  # side-effect-free; registers its own hook
        self.raceline = RacingLineDrawer()
        self.sweep = SweepRunner(self)       # side-effect-free; registers its own hook
        self.sim.add_disconnect_hook(self.motion.stop)

    # -- lifecycle (ties the MotionSim listener to the BeamNGpy connection) ---
    def connect(self, **kwargs: object) -> dict:
        result = self.sim.connect(**kwargs)  # type: ignore[arg-type]
        self.motion.start()
        return result

    def disconnect(self) -> dict:
        return self.sim.disconnect()  # the disconnect hook stops self.motion

    def reconnect(self) -> dict:
        result = self.sim.reconnect()
        self.motion.start()
        return result

    # -- race engineer orchestration ------------------------------------------
    def race_engineer(
        self, feedback: str, lap_path: str | None = None, analyze: bool = True
    ) -> dict:
        """Driver feel + the recorded lap -> a ranked ``$var`` setup plan + brief."""
        self.sim.require_connected()
        try:
            full = tuning.get_tuning_full(self.sim)
        except BeamNGError:
            full = {"vars": {}}  # degrade gracefully -- driver-only advice still works

        available: dict = {}
        for k, meta in full["vars"].items():
            try:
                available[k] = float(meta.get("val"))
            except (TypeError, ValueError):
                pass

        report = None
        if analyze:
            path = lap_path or latest_lap(self.settings.logs_dir)
            if path:
                report = analyze_lap(path)
        live_report = report if (report and report.get("ok")) else None

        diag = advisor.diagnose(feedback or "", live_report, available)
        kept: list = []
        for it in diag.get("plan", []):
            meta = full["vars"].get(it.get("var"))
            if not meta:
                kept.append(it)
                continue
            try:
                lo, hi = float(meta["min"]), float(meta["max"])
                lo, hi = min(lo, hi), max(lo, hi)
                if it.get("proposed") is not None:
                    clamped = max(lo, min(hi, it["proposed"]))
                    # The live-range clamp must not REVERSE the move (a current
                    # value outside the car's slider range would do that) — an
                    # item moving against its own rationale is dropped, same
                    # guard as the spec-range clamp in advisor._diagnose.
                    move = clamped - float(it["current"])
                    if move == 0 or (move > 0) != (it.get("dir") == "+"):
                        diag.setdefault("caveats", []).append(
                            f"{it.get('var')}: current {it['current']:g} is outside the "
                            f"car's live range [{lo:g}, {hi:g}], so a '{it.get('dir')}' "
                            "move has no headroom — dropped from the plan.")
                        continue
                    it["proposed"] = clamped
                it["unit"] = meta.get("unit")
                it["title"] = meta.get("title")
            except (TypeError, ValueError, KeyError):
                pass
            kept.append(it)
        diag["plan"] = kept

        brief = advisor.format_report(live_report, diag)
        return {"engineer": advisor.ENGINEER, "brief": brief, "diagnosis": diag,
                "report": report, "tunable_vars": len(available)}

    def apply_setup(
        self, plan: list | None = None, vars: dict | None = None,
        save_as: str | None = None, vid: str | None = None,
    ) -> dict:
        """Apply a ``race_engineer`` plan (or an explicit ``{$var: value}`` map),
        optionally persisting it as a ``.pc`` build."""
        if isinstance(vars, dict) and vars:
            vmap = vars
        elif isinstance(plan, list):
            vmap = advisor.plan_to_vars(plan, {})
        else:
            vmap = None
        if not vmap:
            raise BeamNGError('nothing to apply: pass plan=[...] or vars={"$var": value}')
        res = tuning.set_tuning_vars(self.sim, vmap, vid=vid)
        if save_as:
            res["saved"] = tuning.save_config(self.sim, save_as, vid=res.get("vid"))
        return res


#: Process-wide App, resolved at import time (side-effect-free construction).
APP = App()
