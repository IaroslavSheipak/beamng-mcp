"""pitwall: the in-game pit board daemon — offline, against a scripted timer.
The contract: every completed lap gets read out in-game; invalid laps are
called out and never coached; Mara's call is advisory-only; the daemon dies
with the connection and never raises out of its thread."""

import threading
import time

from beamng_mcp.errors import BeamNGError
from beamng_mcp.pitwall import PitWallSession


class FakeSim:
    def __init__(self, connected=True):
        self.lock = threading.Lock()
        self.bng = None  # toast() must survive this
        self._connected = connected
        self.hooks = []

    def add_disconnect_hook(self, fn):
        self.hooks.append(fn)

    def require_connected(self):
        if not self._connected:
            raise BeamNGError("not connected; call connect first")


class FakeTimer:
    """Scripted lap-session: laps appear as the test bumps .count."""

    def __init__(self):
        self.count = 0
        self.state = "running"
        self.best = None
        self.laps = []
        self.started = False

    def start_lap_session(self, hz=30.0):
        self.started = True
        return {"state": "running", "note": "fake"}

    def lap_session_status(self):
        return {"state": self.state, "count": self.count,
                "laps": self.laps, "best": self.best}

    def last_lap(self):
        return dict(self.laps[-1])

    def stop_lap_session(self):
        self.state = "stopped"
        return {"state": "stopped", "count": self.count,
                "laps": self.laps, "best": self.best}


class FakeApp:
    def __init__(self, connected=True, plan=None):
        self.sim = FakeSim(connected)
        self.timer = FakeTimer()
        self._plan = plan if plan is not None else []
        self.engineer_calls = []

    def race_engineer(self, feedback, lap_path=None, analyze=True):
        self.engineer_calls.append(lap_path)
        return {"ok": True, "diagnosis": {"plan": self._plan}}


def _session(app):
    s = PitWallSession(app)
    s.POLL_S = 0.02
    toasts = []
    s.toast = toasts.append  # capture instead of hitting a (fake) game
    return s, toasts


def _valid_lap(n, t="1:42.310", csv="lap_x.csv"):
    return {"num": n, "lap_time": t, "lap_time_s": 102.31, "csv": csv,
            "report": {"ok": True, "valid": True,
                       "balance": {"tendency": "oversteer (loose)", "understeer_index": -0.31},
                       "grip": {"envelope_g": 1.24}, "validity": {"reasons": []}}}


def _invalid_lap(n):
    return {"num": n, "lap_time": "0:41.000", "lap_time_s": 41.0, "csv": "lap_y.csv",
            "report": {"ok": True, "valid": False, "balance": {}, "grip": {},
                       "validity": {"reasons": ["car stopped during the lap"]}}}


def _wait(pred, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_start_requires_connection():
    app = FakeApp(connected=False)
    s, _ = _session(app)
    try:
        s.start()
    except BeamNGError as exc:
        assert "not connected" in str(exc)
    else:
        raise AssertionError("must refuse to start without a connection")


def test_valid_lap_gets_time_read_and_maras_call():
    plan = [{"lever": "arb_R", "var": "$arb_spring_R", "current": 30000.0,
             "proposed": 26400.0, "confidence": "medium"}]
    app = FakeApp(plan=plan)
    s, toasts = _session(app)
    out = s.start()
    assert out["state"] == "running" and app.timer.started
    app.timer.laps.append(_valid_lap(1))
    app.timer.best = "1:42.310"
    app.timer.count = 1
    assert _wait(lambda: s.status()["laps_read"] == 1)
    joined = "\n".join(toasts)
    assert "LAP 1  1:42.310" in joined
    assert "oversteer" in joined and "1.24 g" in joined
    assert "MARA" in joined and "$arb_spring_R" in joined and "apply" in joined
    assert app.engineer_calls == ["lap_x.csv"]  # advisory only — nothing applied
    last = s.status()["last_read"]
    assert last["mara_p1"]["proposed"] == 26400.0
    s.stop()


def test_invalid_lap_is_called_out_and_never_coached():
    app = FakeApp(plan=[{"lever": "x", "var": "$x", "current": 1.0,
                         "proposed": 2.0, "confidence": "low"}])
    s, toasts = _session(app)
    s.start()
    app.timer.laps.append(_invalid_lap(1))
    app.timer.count = 1
    assert _wait(lambda: s.status()["laps_read"] == 1)
    joined = "\n".join(toasts)
    assert "invalid lap" in joined and "not coaching" in joined
    assert "MARA:" not in joined  # the setup-call toast; the greeting is fine
    assert app.engineer_calls == []  # no engineer run on garbage data
    s.stop()


def test_double_start_refused_and_stop_summarizes():
    app = FakeApp()
    s, toasts = _session(app)
    s.start()
    try:
        s.start()
    except BeamNGError as exc:
        assert "already running" in str(exc)
    else:
        raise AssertionError("second start must be refused")
    out = s.stop()
    assert out["state"] == "stopped"
    assert app.timer.state == "stopped"
    assert any("PIT SESSION OVER" in t for t in toasts)


def test_daemon_stops_when_underlying_session_dies():
    app = FakeApp()
    s, _ = _session(app)
    s.start()
    app.timer.state = "error"  # e.g. stop_lap_session called directly
    assert _wait(lambda: s.status()["state"] == "stopped")


def test_disconnect_hook_registered_and_stops_daemon():
    app = FakeApp()
    s, _ = _session(app)
    assert s.shutdown in app.sim.hooks
    s.start()
    s.shutdown()
    assert s.status()["state"] == "stopped"


def test_toast_never_raises_without_a_game():
    app = FakeApp()
    s = PitWallSession(app)  # real toast(), sim.bng is None
    s.toast("no game attached")  # must not raise
