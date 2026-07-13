import os
import time
from types import SimpleNamespace

import pytest

from beamng_mcp.errors import BeamNGError
from beamng_mcp.sim.context import Simulator
from beamng_mcp.timing import statemachine
from beamng_mcp.timing.line import StartLine
from beamng_mcp.timing.statemachine import LapTimer, _motion_fields, fmt_time


def _timer(tmp_path):
    return LapTimer(Simulator(), str(tmp_path))


def test_fmt_time():
    assert fmt_time(65.891) == "1:05.891"
    assert fmt_time(5.0) == "0:05.000"
    assert fmt_time(125.5) == "2:05.500"


def test_busy_idle(tmp_path):
    assert _timer(tmp_path).busy() is None


def test_busy_reflects_modes(tmp_path):
    t = _timer(tmp_path)
    t._tt = {"state": "running"}
    assert t.busy() == "time_trial"
    t._tt = {"state": "idle"}
    t._sess = {"state": "running"}
    assert t.busy() == "lap_session"


def test_busy_reflects_recorder(tmp_path):
    t = _timer(tmp_path)
    t.recorder.start(lambda: {"speed": 0.0}, hz=20.0)
    try:
        assert t.busy() == "lap"
    finally:
        t.recorder.stop()


def test_start_lap_rejected_when_busy(tmp_path):
    # The whole point of the redesign: a second mode is refused BEFORE any game
    # call, so the three modes can never share the recorder.
    t = _timer(tmp_path)
    t._sess = {"state": "running"}
    with pytest.raises(BeamNGError):
        t.start_lap()


def test_start_time_trial_rejected_when_lap_running(tmp_path):
    t = _timer(tmp_path)
    t.recorder.start(lambda: {"speed": 0.0}, hz=20.0)
    try:
        with pytest.raises(BeamNGError):
            t.start_time_trial()
    finally:
        t.recorder.stop()


def test_last_lap_empty_raises(tmp_path):
    with pytest.raises(BeamNGError):
        _timer(tmp_path).last_lap()


def test_status_idle_shapes(tmp_path):
    t = _timer(tmp_path)
    assert t.time_trial_status()["state"] == "idle"
    s = t.lap_session_status()
    assert s["state"] == "idle" and s["count"] == 0 and s["best"] is None


def test_motion_fields_none_listener_is_empty():
    assert _motion_fields(None) == {}


def test_motion_fields_no_fresh_packet_is_empty():
    class _NoPacket:
        def latest(self):
            return None

    assert _motion_fields(_NoPacket()) == {}


def test_motion_fields_extracts_yaw_rate_and_accel():
    class _FakeListener:
        def latest(self):
            return {"acc": (1.0, 2.0, 3.0), "ang_vel": (0.1, 0.2, 0.3)}

    out = _motion_fields(_FakeListener())
    assert out == {"ms_yaw_rate": 0.3, "ms_ax": 1.0, "ms_ay": 2.0, "ms_az": 3.0}


def test_motion_field_names_are_recorder_columns():
    # The recorder must have a CSV column for every key _motion_fields can emit.
    from beamng_mcp.timing.recorder import RICH_FIELDS

    class _FakeListener:
        def latest(self):
            return {"acc": (0.0, 0.0, 0.0), "ang_vel": (0.0, 0.0, 0.0)}

    assert set(_motion_fields(_FakeListener())) <= set(RICH_FIELDS)


# --------------------------------------------------------------------------- #
# harness: a real worker over a scripted car (the pitwall/optimizer Fake style)
# — the abort path, the session-distance gate, the idle guard
# --------------------------------------------------------------------------- #
class FakeVehicle:
    """Enough of a beamngpy Vehicle for _poll_rich/_read_kin: scripted state."""

    def __init__(self):
        self.state = {"pos": [-50.0, 0.0, 0.0], "vel": [0.0, 0.0, 0.0],
                      "dir": [1.0, 0.0, 0.0]}
        self.sensors = {"electrics": {}, "gforces": {}}

    def is_connected(self):
        return True

    def poll_sensors(self):
        pass


def _harness(tmp_path, analyze=None):
    """LapTimer against a fake game: the line is the x=0 plane, +x forward."""
    sim = Simulator()
    sim.bng = SimpleNamespace(
        vehicles=SimpleNamespace(get_player_vehicle_id=lambda: {"vid": "car"}))
    veh = FakeVehicle()
    sim.vehicles["car"] = veh
    t = LapTimer(sim, str(tmp_path), analyze=analyze)
    t.line = StartLine(pos=[0.0, 0.0, 0.0], heading=[1.0, 0.0, 0.0])
    return t, veh


def _running_sess():
    return {"state": "running", "lap": 0, "t_cross": None, "best": None,
            "distances": []}


def _wait(pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def _lap_csvs(tmp_path):
    return [f for f in os.listdir(tmp_path) if f.startswith("lap_")]


def _cross(t, veh, timeout=6.0):
    """Script one line crossing: arm far behind the x=0 plane, then step past it
    (retried until a worker tick has seen both positions)."""
    before = t._sess.get("t_cross")
    deadline = time.time() + timeout
    while time.time() < deadline:
        veh.state["pos"] = [-50.0, 0.0, 0.0]
        time.sleep(0.3)
        veh.state["pos"] = [5.0, 0.0, 0.0]
        if _wait(lambda: t._sess.get("t_cross") not in (None, before), timeout=0.5):
            return True
    return False


def test_abort_current_lap_idle_is_a_noop(tmp_path):
    out = _timer(tmp_path).abort_current_lap("setup apply")
    assert out == {"aborted": False, "mode": None, "session": "idle",
                   "discarded": False, "reason": "setup apply"}


def test_abort_current_lap_discards_partial_and_rearms(tmp_path):
    t, veh = _harness(tmp_path)
    veh.state["vel"] = [30.0, 0.0, 0.0]  # driving — the idle guard stays quiet
    t.start_lap_session(hz=50.0)
    try:
        assert _wait(lambda: t.recorder.running)
        assert _cross(t, veh)  # a lap is now open
        assert _wait(lambda: t.recorder.running)
        open_csv = t.recorder.path
        out = t.abort_current_lap("setup apply (respawn)")
        assert out["aborted"] and out["discarded"] and out["session"] == "re-armed"
        assert not os.path.exists(open_csv)  # never left behind as lap_*.csv
        assert t._sess["state"] == "running" and t._sess["t_cross"] is None
        assert _wait(lambda: t.recorder.running)  # worker re-armed + resumed
        assert _cross(t, veh)  # next crossing opens a clean lap
        assert t._laps == []  # the aborted partial never became a lap
    finally:
        t.stop_lap_session()


def test_abort_current_lap_cancels_time_trial(tmp_path):
    t, veh = _harness(tmp_path)
    veh.state["vel"] = [30.0, 0.0, 0.0]
    t.start_time_trial(countdown=0, hz=50.0)
    assert _wait(lambda: t.time_trial_status()["state"] == "running")
    out = t.abort_current_lap("setup apply (respawn)")
    assert out["aborted"] and out["discarded"] and out["session"] == "idle"
    assert t.time_trial_status()["state"] == "aborted"
    assert not _lap_csvs(tmp_path)


def test_session_distance_outlier_marks_lap_invalid(tmp_path):
    # The live failure: a respawn-truncated 857 m "lap" on a ~1100 m circuit
    # passed per-lap validity and stole "best".
    t, _ = _harness(tmp_path)
    t._sess = _running_sess()
    t._close_lap(0.0, {})  # first crossing: opens lap 1, closes nothing
    for i, d in enumerate([1100.0, 1105.0, 1095.0], start=1):
        t._close_lap(i * 90.0, {"path": f"lap_{i}.csv", "distance_m": d})
    t._close_lap(330.0, {"path": "lap_4.csv", "distance_m": 857.0})  # a 60 s "lap"
    st = t.lap_session_status()
    assert [x["valid"] for x in st["laps"]] == [True, True, True, False]
    reason = st["laps"][3]["invalid_reason"]
    assert "857" in reason and "median" in reason and "1100" in reason
    assert st["median_distance_m"] == 1100.0  # the outlier never joined the pool
    assert st["best"] == "1:30.000"  # ... and never took best despite being fastest


def test_distance_gate_needs_two_banked_laps(tmp_path):
    t, _ = _harness(tmp_path)
    t._sess = _running_sess()
    t._close_lap(0.0, {})
    t._close_lap(90.0, {"path": "lap_1.csv", "distance_m": 857.0})
    st = t.lap_session_status()
    assert st["laps"][0]["valid"] and "median_distance_m" not in st
    t._close_lap(180.0, {"path": "lap_2.csv", "distance_m": 1100.0})
    assert t.lap_session_status()["laps"][1]["valid"]  # pool had 1 — not judged


def test_last_lap_carries_session_distance_verdict_into_report(tmp_path):
    t, _ = _harness(
        tmp_path,
        analyze=lambda p: {"ok": True, "valid": True,
                           "validity": {"valid": True, "reasons": []}})
    t._sess = _running_sess()
    t._close_lap(0.0, {})
    for i, d in enumerate([1100.0, 1102.0, 857.0], start=1):
        t._close_lap(i * 90.0, {"path": f"lap_{i}.csv", "distance_m": d})
    out = t.last_lap()
    assert out["valid"] is False and "857" in out["invalid_reason"]
    rep = out["report"]  # the surface the pit board reads — it will never coach this
    assert rep["valid"] is False
    assert any("857" in r for r in rep["validity"]["reasons"])


def test_idle_guard_aborts_parked_open_lap(tmp_path, monkeypatch):
    monkeypatch.setattr(statemachine, "IDLE_ABORT_S", 0.3)
    t, veh = _harness(tmp_path)  # vel 0: parked from the first sample
    t.start_lap_session(hz=50.0)
    try:
        assert _wait(lambda: t.recorder.running)
        t._sess["t_cross"] = time.monotonic()  # a lap is open while the car sits
        assert _wait(lambda: not t.recorder.running)  # the guard fired
        assert t._sess["state"] == "running"  # session survives, still armed
        assert t._sess["t_cross"] is None
        assert t._laps == []
        assert not _lap_csvs(tmp_path)  # the phantom recording was discarded
        veh.state["vel"] = [20.0, 0.0, 0.0]  # wake up: recording resumes
        assert _wait(lambda: t.recorder.running)
    finally:
        t.stop_lap_session()


def test_idle_guard_aborts_time_trial(tmp_path, monkeypatch):
    monkeypatch.setattr(statemachine, "IDLE_ABORT_S", 0.3)
    t, _ = _harness(tmp_path)  # parked
    t.start_time_trial(countdown=0, hz=50.0)
    assert _wait(lambda: t.time_trial_status()["state"] == "aborted")
    st = t.time_trial_status()
    assert "parked" in st["reason"] and "discarded" in st["reason"]
    assert not _lap_csvs(tmp_path)


# --------------------------------------------------------------------------- #
# regression: the "LAP 20 with zero CSVs" live failure (2026-07-13)
# — a dead recorder must fail the session loud, never time laps blind;
# — a crossing with no recording behind it must never register a lap;
# — a respawn teleport right after a re-arm must not read as a crossing.
# --------------------------------------------------------------------------- #
def test_session_errors_when_recorder_cannot_start(tmp_path):
    t, veh = _harness(tmp_path)

    def _broken_poll():
        raise RuntimeError("wedged vehicle socket")

    t._poll_rich = _broken_poll  # first poll kills the recorder instantly
    veh.state["vel"] = [30.0, 0.0, 0.0]
    t.start_lap_session(hz=50.0)
    assert _wait(lambda: t.lap_session_status()["state"] == "error")
    st = t.lap_session_status()
    assert "recorder" in st["error"] and st["count"] == 0
    assert not _lap_csvs(tmp_path)


def test_recorder_death_mid_session_fails_loud_not_blind(tmp_path):
    t, veh = _harness(tmp_path)
    veh.state["vel"] = [30.0, 0.0, 0.0]
    real_poll = t._poll_rich
    broken = {"on": False}

    def _flaky_poll():
        if broken["on"]:
            raise RuntimeError("wedged mid-session")
        return real_poll()

    t._poll_rich = _flaky_poll
    t.start_lap_session(hz=50.0)
    try:
        assert _wait(lambda: t.recorder.running)
        broken["on"] = True  # the recorder dies on its next poll
        assert _wait(lambda: t.lap_session_status()["state"] == "error", timeout=5.0)
        # ... and the worker must NOT have kept closing laps off direct reads
        assert t.lap_session_status()["count"] == 0
    finally:
        t.stop_lap_session()


def test_crossing_without_recording_registers_no_lap(tmp_path):
    t, _ = _harness(tmp_path)
    t._sess = _running_sess()
    t._close_lap(0.0, {"ok": True, "path": "x.csv", "distance_m": 1000.0})  # opens
    t._close_lap(90.0, {"ok": False, "error": "no active recording"})  # dead stop
    st = t.lap_session_status()
    assert st["count"] == 0
    assert st["discarded_crossings"] == 1
    assert t._sess["t_cross"] == 90.0  # the crossing still re-opens the next lap


def test_teleport_within_grace_after_rearm_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(statemachine, "CROSS_GRACE_S", 0.6)
    t, veh = _harness(tmp_path)
    veh.state["vel"] = [30.0, 0.0, 0.0]
    t.start_lap_session(hz=50.0)
    try:
        assert _wait(lambda: t.recorder.running)
        assert _cross(t, veh)  # lap 1 opens normally
        t.abort_current_lap("setup apply (respawn)")
        assert _wait(lambda: t.recorder.running)  # resumed
        # a respawn teleport: far behind the plane, then across it, within grace
        veh.state["pos"] = [-50.0, 0.0, 0.0]
        time.sleep(0.15)
        veh.state["pos"] = [5.0, 0.0, 0.0]
        time.sleep(0.2)  # still inside the 0.6 s grace window
        assert t._sess.get("t_cross") is None  # the teleport did NOT count
        time.sleep(0.6)  # grace over — real crossings work again
        assert _cross(t, veh)
    finally:
        t.stop_lap_session()
