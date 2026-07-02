"""optimizer: the space must refuse untrustworthy levers, the strategy must
actually optimize a synthetic objective under its budget, and the runner must
survive the night — ledger written, rails firing, best config restored."""

import json
import threading
import time

import pytest

from beamng_mcp.errors import BeamNGError
from beamng_mcp.optimizer.runner import SweepRunner
from beamng_mcp.optimizer.search import Eval, Strategy
from beamng_mcp.optimizer.space import build_space

# --------------------------------------------------------------------------- #
# space
# --------------------------------------------------------------------------- #
TUNING = {
    "$arb_spring_F": {"val": 45000, "min": 2000, "max": 200000},   # KB clamps hi
    "$arb_spring_R": {"val": 30000, "min": 10000, "max": 60000},
    "$lsdlockcoef_R": {"val": 0.1, "min": 0.0, "max": 0.5},
    "$brakebias": {"val": 0.68, "min": 0.4, "max": 0.9},
    "$camber_R": {"val": 0.99, "min": 0.95, "max": 1.05},          # angle_mult
    "$tirepressure_F": {"val": 28, "min": 0, "max": 40},           # pressure
    "$mystery_knob": {"val": 5, "min": 0, "max": 10},              # no KB spec
    "$spring_F": {"val": 80000, "min": 80000, "max": 80000},       # degenerate
}


def test_auto_space_excludes_untrustworthy_levers():
    space = build_space(TUNING)
    names = {p.var for p in space}
    assert "$camber_R" not in names        # angle_mult: direction untrustworthy
    assert "$tirepressure_F" not in names  # pressure: live-apply path only
    assert "$mystery_knob" not in names    # unknown to the KB
    assert "$spring_F" not in names        # degenerate range
    assert {"$arb_spring_F", "$arb_spring_R", "$lsdlockcoef_R", "$brakebias"} <= names


def test_space_intersects_live_range_with_kb_clamp():
    space = build_space(TUNING, include=["$arb_spring_F"])
    p = space[0]
    assert p.hi == 100000  # KB clamp, not the jbeam 200000
    assert p.lo == 2000


def test_include_unknown_var_is_a_clean_error():
    with pytest.raises(BeamNGError, match="not a tunable"):
        build_space(TUNING, include=["$nope"])


# --------------------------------------------------------------------------- #
# strategy on a synthetic objective
# --------------------------------------------------------------------------- #
def _optimize(objective, budget=24, seed=7, space_vars=None):
    space = build_space(space_vars or TUNING,
                        include=["$arb_spring_R", "$lsdlockcoef_R"])
    strat = Strategy(space=space, budget=budget, seed=seed)
    history: list[Eval] = []
    while True:
        cand = strat.propose(history)
        if cand is None:
            break
        history.append(Eval(vars=cand, objective=objective(cand)))
    return strat, history


def test_first_eval_is_the_baseline():
    _, history = _optimize(lambda v: 60.0, budget=3)
    assert history[0].vars == {"$arb_spring_R": 30000, "$lsdlockcoef_R": 0.1}


def test_strategy_respects_budget():
    _, history = _optimize(lambda v: 60.0, budget=9)
    assert len(history) <= 9


def test_strategy_finds_a_quadratic_optimum():
    target = {"$arb_spring_R": 42000.0, "$lsdlockcoef_R": 0.35}

    def objective(v):
        return (60.0
                + ((v["$arb_spring_R"] - target["$arb_spring_R"]) / 50000) ** 2 * 40
                + ((v["$lsdlockcoef_R"] - target["$lsdlockcoef_R"]) / 0.5) ** 2 * 40)

    strat, history = _optimize(objective, budget=30)
    best = strat.best(history)
    baseline = history[0]
    assert best.objective < baseline.objective  # it must actually improve
    assert abs(best.vars["$arb_spring_R"] - 42000) < 12000
    assert abs(best.vars["$lsdlockcoef_R"] - 0.35) < 0.15


def test_strategy_is_deterministic_under_a_seed():
    f = lambda v: 60.0 + v["$lsdlockcoef_R"] * 10  # noqa: E731
    _, h1 = _optimize(f, budget=12, seed=42)
    _, h2 = _optimize(f, budget=12, seed=42)
    assert [e.vars for e in h1] == [e.vars for e in h2]


def test_failed_evals_do_not_crash_the_strategy():
    calls = {"n": 0}

    def objective(v):
        calls["n"] += 1
        return None if calls["n"] % 3 == 0 else 60.0 + calls["n"] * 0.01

    _, history = _optimize(objective, budget=15)
    assert len(history) >= 6  # kept proposing despite None objectives


# --------------------------------------------------------------------------- #
# runner against a fake harness
# --------------------------------------------------------------------------- #
class FakeSim:
    def __init__(self):
        self.lock = threading.Lock()
        self.bng = None
        self.hooks = []

    def add_disconnect_hook(self, fn):
        self.hooks.append(fn)

    def require_connected(self):
        pass


class FakeTimer:
    """Line-crossing session that manufactures lap times on demand."""

    def __init__(self):
        self.line = object()   # a start line exists
        self.lap_fn = None     # set by the harness: () -> (time_s, valid)
        self._running = False
        self._count = 0
        self._last = None

    def busy(self):
        return None

    def set_start_line(self):
        return {}

    def start_lap_session(self, hz=30.0):
        self._running = True
        self._count = 0
        return {"state": "running"}

    def lap_session_status(self):
        if self._running and self.lap_fn is not None:
            t, valid = self.lap_fn()
            self._count += 1
            self._last = {"lap_time_s": t,
                          "report": {"valid": valid, "ok": True}}
        return {"state": "running" if self._running else "stopped",
                "count": self._count}

    def last_lap(self):
        return dict(self._last)

    def stop_lap_session(self):
        self._running = False
        return {"state": "stopped", "count": self._count}


class FakeApp:
    def __init__(self, tmp_path):
        self.sim = FakeSim()
        self.timer = FakeTimer()
        self.settings = type("S", (), {"logs_dir": str(tmp_path)})()


class HarnessRunner(SweepRunner):
    """Game-facing primitives replaced; physics = a quadratic lap-time model."""

    POLL_S = 0.001
    target = 42000.0

    def __init__(self, app, fail_all=False):
        super().__init__(app)
        self.applied: list[dict] = []
        self.robot_calls: list[bool] = []
        self.fail_all = fail_all
        app.timer.lap_fn = self._lap

    def _lap(self):
        if self.fail_all:
            return 200.0, False
        v = self.applied[-1]["$arb_spring_R"]
        return 60.0 + ((v - self.target) / 50000.0) ** 2 * 40, True

    def _toast(self, msg):
        pass

    def _tuning_full(self):
        return {"vars": TUNING}

    def _apply(self, vars_map):
        self.applied.append(dict(vars_map))

    def _robot(self, on, speed_kmh, aggression):
        self.robot_calls.append(on)

    def _speed_ms(self):
        return 10.0


def _wait_done(runner, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if runner.status()["state"] in ("done", "aborted"):
            return True
        time.sleep(0.02)
    return False


def test_sweep_improves_and_restores_best(tmp_path):
    app = FakeApp(tmp_path)
    r = HarnessRunner(app)
    out = r.start(vars=["$arb_spring_R"], configs=14, laps_per_config=2, minutes=5)
    assert out["state"] == "running"
    assert _wait_done(r)
    st = r.status()
    assert st["state"] == "done"
    assert st["best"]["lap_time_s"] < st["baseline_lap_time_s"]
    assert st["gain_s"] > 0
    # the LAST apply is the restore of the best config
    assert r.applied[-1] == st["best"]["vars"]
    # robot turned off at the end
    assert r.robot_calls[-1] is False
    # ledger has one line per eval, json-parsable
    lines = open(st["ledger"], encoding="utf-8").read().splitlines()
    assert len(lines) == st["eval"]
    assert all("objective" in json.loads(ln) for ln in lines)


def test_sweep_aborts_after_two_failed_configs(tmp_path):
    app = FakeApp(tmp_path)
    r = HarnessRunner(app, fail_all=True)
    r.start(vars=["$arb_spring_R"], configs=10, laps_per_config=2, minutes=5)
    assert _wait_done(r)
    st = r.status()
    assert st["state"] == "aborted"
    assert st["eval"] == 2  # exactly the two strikes


def test_sweep_refuses_when_timing_is_busy(tmp_path):
    app = FakeApp(tmp_path)
    app.timer.busy = lambda: "pit_session"
    r = HarnessRunner(app)
    with pytest.raises(BeamNGError, match="busy"):
        r.start(vars=["$arb_spring_R"])


def test_stop_mid_sweep_still_restores(tmp_path):
    app = FakeApp(tmp_path)
    r = HarnessRunner(app)
    slow = threading.Event()
    orig = r._lap

    def slow_lap():
        slow.set()
        time.sleep(0.05)
        return orig()

    r._lap = slow_lap
    app.timer.lap_fn = slow_lap
    r.start(vars=["$arb_spring_R"], configs=50, laps_per_config=2, minutes=5)
    assert slow.wait(5.0)
    st = r.stop()
    assert st["state"] in ("done", "aborted", "stopped") or st["eval"] < 50
    assert r.robot_calls[-1] is False  # robot never left running


def test_double_start_refused(tmp_path):
    app = FakeApp(tmp_path)
    r = HarnessRunner(app)
    r.start(vars=["$arb_spring_R"], configs=30, laps_per_config=2, minutes=5)
    try:
        with pytest.raises(BeamNGError, match="already running"):
            r.start(vars=["$arb_spring_R"])
    finally:
        r.stop()
